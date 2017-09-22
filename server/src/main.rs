// Copyright (c) 2004-present, Facebook, Inc.
// All Rights Reserved.
//
// This software may be used and distributed according to the terms of the
// GNU General Public License version 2 or any later version.

#![deny(warnings)]
// TODO: (sid0) T21726029 tokio/futures deprecated a bunch of stuff, clean it all up
#![allow(deprecated)]

extern crate bytes;
#[macro_use]
extern crate futures;
extern crate tokio_core;
extern crate tokio_io;
extern crate tokio_uds;

extern crate clap;

#[macro_use]
extern crate error_chain;

#[macro_use]
extern crate slog;
extern crate slog_kvfilter;
extern crate slog_term;

#[macro_use]
extern crate maplit;

extern crate async_compression;
extern crate blobrepo;
extern crate futures_ext;
extern crate hgproto;
extern crate mercurial;
extern crate mercurial_bundles;
extern crate mercurial_types;
extern crate metaconfig;
extern crate services;
extern crate sshrelay;

use std::io;
use std::panic;
use std::path::Path;
use std::str::FromStr;
use std::sync::Arc;
use std::thread;

use futures::{Future, Sink, Stream};
use futures::sink::Wait;
use futures::sync::mpsc;

use clap::{App, Arg, ArgGroup};

use slog::{Drain, Level, LevelFilter, Logger};
use slog_kvfilter::KVFilter;

use bytes::Bytes;
use futures_ext::{encode, StreamLayeredExt};
use hgproto::HgService;
use hgproto::sshproto::{HgSshCommandDecode, HgSshCommandEncode};
use metaconfig::RepoConfigs;
use metaconfig::repoconfig::RepoType;

mod errors;
mod repo;
mod listener;

use errors::*;

use listener::{ssh_server_mux, Stdio};
use repo::OpenableRepoType;

struct SenderBytesWrite {
    chan: Wait<mpsc::Sender<Bytes>>,
}

impl io::Write for SenderBytesWrite {
    fn flush(&mut self) -> io::Result<()> {
        self.chan
            .flush()
            .map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, e))
    }

    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.chan
            .send(Bytes::from(buf))
            .map(|_| buf.len())
            .map_err(|e| io::Error::new(io::ErrorKind::BrokenPipe, e))
    }
}

// Listener thread for a specific repo
fn repo_listen(sockname: &Path, repo: repo::HgRepo, listen_log: &Logger) -> Result<()> {
    let mut core = tokio_core::reactor::Core::new()?;
    let handle = core.handle();
    let repo = Arc::new(repo);

    let server = listener::listener(sockname, &handle)?
        .map_err(Error::from)
        .for_each(move |sock| {
            match sock.peer_addr() {
                Ok(addr) => info!(listen_log, "New connection from {:?}", addr),
                Err(err) => error!(listen_log, "Failed to get peer addr: {}", err),
            };

            // Have a connection. Extract std{in,out,err} streams for socket
            let Stdio {
                stdin,
                stdout,
                stderr,
            } = ssh_server_mux(sock, &handle);

            let stderr_write = SenderBytesWrite {
                chan: stderr.clone().wait(),
            };
            let drain = slog_term::PlainSyncDecorator::new(stderr_write);
            let drain = slog_term::FullFormat::new(drain).build();
            let drain = KVFilter::new(
                drain,
                Level::Critical,
                hashmap! {
                    "remote".into() => hashset!["true".into()],
                },
            );
            let drain = slog::Duplicate::new(drain, listen_log.clone()).fuse();
            let conn_log = slog::Logger::root(drain, o![]);

            // Construct a repo
            let client = repo::RepoClient::new(repo.clone(), &conn_log);
            let service = Arc::new(HgService::new_with_logger(client, &conn_log));

            // Map stdin into mercurial requests
            let reqs = stdin.decode(HgSshCommandDecode);

            // process requests
            let resps = reqs.and_then(move |req| service.clone().command(req));

            // send responses back
            let endres = encode::encode(resps, HgSshCommandEncode)
                .map_err(Error::from)
                .forward(stdout)
                .map(|_| ());

            // If we got an error at this point, then catch it, print a message and return
            // Ok (if we allow the Error to propagate further it will shutdown the listener
            // rather than just the connection). Unfortunately there's no way to print what the
            // actual failing command was.
            // TODO: seems to leave the client hanging?
            let conn_log = conn_log.clone();
            let endres = endres.or_else(move |err| {
                error!(conn_log, "Command failed: {}", err; "remote" => "true");

                for e in err.iter().skip(1) {
                    error!(conn_log, "caused by: {}", e; "remote" => "true");
                }
                Ok(())
            });

            // Run the whole future asynchronously to allow new connections
            handle.spawn(endres);

            Ok(())
        });

    core.run(server)?;

    Ok(())
}

fn run<'a, I>(repos: I, root_log: &Logger) -> Result<()>
where
    I: IntoIterator<Item = RepoType>,
{
    // Given the list of paths to repos:
    // - initialize the repo
    // - create a thread for it
    // - wait for connections in that thread
    let threads = repos
        .into_iter()
        .map(|repotype| {
            repo::init_repo(root_log, &repotype).and_then(move |(sockname, repo)| {
                let repopath = repo.path().clone();
                let listen_log = root_log.new(o!("repo" => repopath.clone()));
                info!(listen_log, "Listening for connections");

                // start a thread for each repo to own the reactor and start listening for
                // connections
                let t = thread::spawn(move || {
                    // Not really sure this is actually Unwind Safe
                    // (future version of slog will make this explicit)
                    let unw = panic::catch_unwind(panic::AssertUnwindSafe(
                        || repo_listen(&sockname, repo, &listen_log),
                    ));
                    match unw {
                        Err(err) => {
                            crit!(
                                listen_log,
                                "Listener thread {} paniced: {:?}",
                                repopath,
                                err
                            );
                            Ok(())
                        }
                        Ok(v) => v,
                    }
                });
                Ok(t)
            })
        })
        .collect::<Vec<_>>();

    // Check for an report any repo initialization errors
    for err in threads.iter().filter_map(|t| t.as_ref().err()) {
        error!(root_log, "{}", err);
        for chain_link in err.iter().skip(1) {
            error!(root_log, "Reason: {}", chain_link)
        }
    }

    // Wait for all threads, and report any problem they have
    for thread in threads.into_iter().filter_map(Result::ok) {
        if let Err(err) = thread.join().expect("thread join failed") {
            error!(root_log, "Listener failure: {:?}", err);
        }
    }

    Ok(())
}

fn main() {
    let matches = App::new("mononoke server")
        .version("0.0.0")
        .about("serve repos")
        .args_from_usage("[debug] -d, --debug     'print debug level output'")
        .arg(
            Arg::with_name("thrift_port")
                .long("thrift_port")
                .short("p")
                .takes_value(true)
                .help("if provided then thrift server will start on this port"),
        )
        .arg(
            Arg::with_name("crpath")
                .long("configrepo_path")
                .short("P")
                .takes_value(true)
                .required(true)
                .help("path to the config repo"),
        )
        .arg(
            Arg::with_name("crbookmark")
                .long("configrepo_bookmark")
                .short("B")
                .takes_value(true)
                .help("config repo bookmark"),
        )
        .arg(
            Arg::with_name("crhash")
                .long("configrepo_commithash")
                .short("C")
                .takes_value(true)
                .help("config repo commit hash"),
        )
        .group(
            ArgGroup::default()
                .args(&["crbookmark", "crhash"])
                .required(true),
        )
        .get_matches();

    let level = if matches.is_present("debug") {
        Level::Debug
    } else {
        Level::Info
    };

    // TODO: switch to TermDecorator, which supports color
    let drain = slog_term::PlainSyncDecorator::new(io::stdout());
    let drain = slog_term::FullFormat::new(drain).build();
    let drain = LevelFilter::new(drain, level).fuse();
    let root_log = slog::Logger::root(drain, o![]);

    info!(root_log, "Starting up");

    let thrift_server = matches.value_of("thrift_port").map(|port| {
        let port = port.parse().expect("Failed to parse thrift_port as number");
        services::init_service_framework(
            "mononoke_server",
            port,
            0, // Disables separate status http server
        )
    });

    let config_repo = RepoType::Revlog(matches.value_of("crpath").unwrap().into())
        .open()
        .unwrap();

    let node_hash = if let Some(bookmark) = matches.value_of("crbookmark") {
        config_repo
            .get_bookmarks()
            .expect("Failed to get bookmarks for config repo")
            .get(&bookmark)
            .wait()
            .expect("Error while looking up bookmark for config repo")
            .expect("Bookmark for config repo not found")
            .0
    } else {
        mercurial_types::NodeHash::from_str(matches.value_of("crhash").unwrap())
            .expect("Commit for config repo not found")
    };

    info!(
        root_log,
        "Config repository will be read from commit: {}",
        node_hash
    );

    let config = RepoConfigs::read_config_repo(config_repo, node_hash)
        .wait()
        .unwrap();

    if let Err(ref e) = run(config.repos.into_iter().map(|(_, c)| c.repotype), &root_log) {
        error!(root_log, "Failed: {}", e);

        for e in e.iter().skip(1) {
            error!(root_log, "caused by: {}", e);
        }

        std::process::exit(1);
    }

    if let Some(handle) = thrift_server {
        handle.join().unwrap().unwrap();
    }
}
