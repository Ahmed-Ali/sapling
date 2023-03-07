/**
 * Portions Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

/*

Copyright (c) 2020 Jun Wu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/

import {diff_match_patch} from 'diff-match-patch';

const dmp = new diff_match_patch();

// The timeout does not seem to affect dmp performance.
// But bumping it produces better diff results.
dmp.Diff_Timeout = 10000;

/** Operation code. */
enum Op {
  /** Unconditional jump. */
  J = 0,
  /** Jump if the current rev >= operand. */
  JGE = 1,
  /** Jump if the current rev < operand. */
  JL = 2,
  /** Append a line. */
  LINE = 3,
  /** End execution. */
  END = 4,
}

/** J instruction. */
interface J {
  /** Opcode: J */
  op: Op.J;
  /** Program counter (offset to jump). */
  pc: Pc;
}

/** JGE instruction. */
interface JGE {
  /** Opcode: JGE */
  op: Op.JGE;
  /** `rev` to test. */
  rev: Rev;
  /** Program counter (offset to jump). */
  pc: Pc;
}

/** JL instruction. */
interface JL {
  /** Opcode: JL */
  op: Op.JL;
  /** `rev` to test. */
  rev: Rev;
  /** Program counter (offset to jump). */
  pc: Pc;
}

/** LINE instruction. */
interface LINE {
  /** Opcode: LINE */
  op: Op.LINE;
  /** `rev` to test. */
  rev: Rev;
  /** Line content. Includes EOL. */
  data: string;
}

/** END instruction. */
interface END {
  /** Opcode: END */
  op: Op.END;
}

/** Program counter (offset to instructions). */
type Pc = number;

/** Revision number. Usually starts from 1. Larger number means newer versions. */
type Rev = number;

/** Index of a line. Starts from 0. */
type LineIdx = number;

/** Instruction. */
type Inst = J | JGE | JL | LINE | END;

/** Information about a line. Internal (`lines`) result of `LineLog.checkOut`. */
interface LineInfo {
  /** Line content. Includes EOL. */
  data: string;
  /** Added by the given rev. */
  rev: Rev;
  /** Produced by the instruction at the given offset. */
  pc: Pc;
  /**
   * Whether the line is deleted.
   * This is always `false` if `checkOut(rev, None)`.
   * It might be `true` when checking out a range of revisions
   * (aka. `start` passed to `checkOut` is not `null`).
   */
  deleted: boolean;
}

/** A "flatten" line. Result of `LineLog.flatten()`. */
interface FlattenLine {
  /** The line is present in the given revisions. */
  revs: Set<Rev>;
  /** Content of the line, including `\n`. */
  data: string;
}

/**
 * `LineLog` is a data structure that tracks linear changes to a single text
 * file. Conceptually similar to a list of texts like `string[]`, with extra
 * features suitable for stack editing:
 * - Calculate the "blame" of the text of a given version efficiently.
 * - Edit lines or trunks in a past version, and affect future versions.
 * - List all lines that ever existed with each line annotated, like
 *   a unified diff, but for all versions, not just 2 versions.
 *
 * Internally, `LineLog` is a byte-code interpreter that runs a program to
 * emit lines. Changes are done by patching in new byte-codes. There are
 * no traditional text patch involved. No operations would cause merge
 * conflicts. See https://sapling-scm.com/docs/internals/linelog for more
 * details.
 */
class LineLog {
  /** Core state: instructions. The array index type is `Pc`. */
  private code: Inst[];

  /**
   * Rev dependencies.
   * For example, `{5: [3, 1]}` means rev 5 depends on rev 3 and rev 1.
   * This is only updated when `trackDeps` is `true` during construction.
   */
  readonly revDepMap: Map<Rev, Set<Rev>>;

  /** If `true`, update `revDepMap` on change. */
  private trackDeps: boolean;

  /** Maximum rev tracked. */
  maxRev: Rev;

  /** Cache key for `checkOut`. */
  private lastCheckoutKey: string;

  /** Result of a `checkOut`. */
  lines: LineInfo[];

  /** Content of lines joined. */
  content: string;

  /**
   * Create a `LineLog` with empty content.
   * If `trackDeps` is `true`, rev dependencies are updated and
   * stored in `revDepMap`.
   */
  constructor({trackDeps}: {trackDeps: boolean} = {trackDeps: false}) {
    this.code = [{op: Op.END}];
    this.revDepMap = new Map<Rev, Set<Rev>>();
    this.maxRev = 0;
    this.lastCheckoutKey = '';
    this.lines = [];
    this.content = '';
    this.trackDeps = trackDeps;
    this.checkOut(0);
  }

  /**
   * Edit chunk. Replace line `a1` (inclusive) to `a2` (exclusive) with
   * `lines`. `lines` are considered introduced by `rev`. If `lines` is
   * empty, the edit is a deletion. If `a1` equals to `a2`, the edit is
   * an insertion. Otherwise, the edit is a modification.
   *
   * `a1` and `a2` are based on the line indexes of the current checkout.
   * Use `checkOut(this.maxRev)` before this function to edit the last
   * revision. Use `checkOut(rev)` before this function to edit arbitary
   * revision.
   *
   * While this function does not cause conflicts or error out, not all
   * editings make practical sense. The callsite might want to do some
   * extra checks to ensure the edit is meaningful.
   */
  private editChunk(a1: LineIdx, a2: LineIdx, rev: Rev, lines: string[]) {
    assert(a1 <= a2, 'illegal chunk (a1 < a2)');
    assert(a2 <= this.lines.length, 'out of bound a2 (forgot checkOut?)');

    // Track dependencies. This is done by marking rev depend on all revs added by the a1..a2 range.
    if (this.trackDeps) {
      let depRevs = this.revDepMap.get(rev);
      if (depRevs == null) {
        const set = new Set<Rev>();
        this.revDepMap.set(rev, set);
        depRevs = set;
      }
      // Also check surrounding lines. This is a bit conservative.
      for (let ai = Math.max(a1 - 1, 0); ai < Math.min(a2 + 1, this.lines.length); ai += 1) {
        const depRev = this.lines[ai].rev;
        if (depRev > 0 && depRev < rev) {
          depRevs.add(depRev);
        }
      }
    }

    const start = this.code.length;
    const a1Pc = this.lines[a1].pc;
    if (lines.length > 0) {
      const b2Pc = start + lines.length + 1;
      this.code.push({op: Op.JL, rev, pc: b2Pc});
      lines.forEach(line => {
        this.code.push({op: Op.LINE, rev, data: line});
      });
      assert(b2Pc === this.code.length, 'bug: wrong pc');
    }
    if (a1 < a2) {
      const a2Pc = this.lines[a2 - 1].pc + 1;
      this.code.push({op: Op.JGE, rev, pc: a2Pc});
    }
    this.lines[a1].pc = this.code.length;
    this.code.push({...this.code[a1Pc]});
    switch (this.code[a1Pc].op) {
      case Op.J:
      case Op.END:
        break;
      default:
        this.code.push({op: Op.J, pc: a1Pc + 1});
    }
    this.code[a1Pc] = {op: Op.J, pc: start};

    const newLines = lines.map((s, i) => {
      return {data: s, rev, pc: start + 1 + i, deleted: false};
    });
    this.lines.splice(a1, a2 - a1, ...newLines);
    if (rev > this.maxRev) {
      this.maxRev = rev;
    }
    // NOTE: this.content is not updated here. It should be updated by the call-site.
  }

  /**
   * Interpret the bytecodes with the given revision range.
   * Used by `checkOut`.
   */
  private execute(
    startRev: Rev,
    endRev: Rev,
    present: {[pc: number]: boolean} | null = null,
  ): LineInfo[] {
    const rev = endRev;
    const lines: LineInfo[] = [];
    let pc = 0;
    let patience = this.code.length * 2;
    const deleted = present === null ? () => false : (pc: Pc) => !present[pc];
    while (patience > 0) {
      const code = this.code[pc];
      switch (code.op) {
        case Op.END:
          lines.push({data: '', rev: 0, pc, deleted: deleted(pc)});
          patience = -1;
          break;
        case Op.LINE:
          lines.push({data: code.data, rev: code.rev, pc, deleted: deleted(pc)});
          pc += 1;
          break;
        case Op.J:
          pc = code.pc;
          break;
        case Op.JGE:
          if (startRev >= code.rev) {
            pc = code.pc;
          } else {
            pc += 1;
          }
          break;
        case Op.JL:
          if (rev < code.rev) {
            pc = code.pc;
          } else {
            pc += 1;
          }
          break;
        default:
          assert(false, 'bug: unknown code');
      }
      patience -= 1;
    }
    if (patience === 0) {
      assert(false, 'bug: code does not end in time');
    }
    return lines;
  }

  /**
   * Flatten lines. Each returned line is associated with a set
   * of `Rev`s, meaning that line is present in those `Rev`s.
   *
   * The returned lines can be useful to figure out file contents
   * after reordering, folding commits. It can also provide a view
   * similar to `absorb -e FILE` to edit all versions of a file in
   * a single view.
   *
   * Note: This is currently implemented naively as roughly
   * `O(lines * revs)`. Avoid calling frequently for large
   * stacks.
   */
  public flatten(): FlattenLine[] {
    this.checkOut(this.maxRev, 0);
    // Drop the last (empty) line.
    const len = Math.max(this.lines.length - 1, 0);
    const lineInfos = this.lines.slice(0, len);
    const linePcs = lineInfos.map(info => info.pc);
    const result: FlattenLine[] = lineInfos.map(info => ({
      revs: new Set<Rev>(),
      data: info.data,
    }));
    for (let rev = 1; rev <= this.maxRev; rev += 1) {
      this.checkOut(rev);
      // Pc is used as the "unique" line identifier to detect what
      // subset of "all lines" exist in the current "rev".
      const pcSet: Set<Pc> = new Set(this.lines.map(info => info.pc));
      for (let i = 0; i < linePcs.length; i += 1) {
        if (pcSet.has(linePcs[i])) {
          result[i].revs.add(rev);
        }
      }
    }
    return result;
  }

  /**
   * Checkout the content of the given revision `rev`.
   *
   * Updates `this.lines` internally so indexes passed to `editChunk`
   * will be based on the given `rev`.
   *
   * If `start` is not `null`, checkout a revision range. For example,
   * if `start` is 0, and `rev` is `this.maxRev`, `this.lines` will
   * include all lines ever existed in all revisions.
   *
   *  @returns Content of the specified revision.
   */
  public checkOut(rev: Rev, start: Rev | null = null): string {
    // eslint-disable-next-line no-param-reassign
    rev = Math.min(rev, this.maxRev);
    const key = `${rev},${start}`;
    if (key === this.lastCheckoutKey) {
      return this.content;
    }

    let lines = this.execute(rev, rev);
    if (start !== null) {
      // Checkout a range, including deleted revs.
      const present: {[key: number]: boolean} = {};
      lines.forEach(l => {
        present[l.pc] = true;
      });

      // Go through all lines again. But do not skip chunks.
      lines = this.execute(start, rev, present);
    }

    this.lines = lines;
    this.content = this.reconstructContent();
    this.lastCheckoutKey = key;
    return this.content;
  }

  private reconstructContent(): string {
    return this.lines.map(l => l.data).join('');
  }

  /**
   * Edit LineLog to match the content of `text`.
   * This might affect `rev`s that are >= `rev` in the stack.
   * Previous revisions won't be affected.
   *
   * @param text Content to match.
   * @param rev Revision to to edit (in-place). If not set, append a new revision.
   * @returns Revision number. `this.checkOut(rev)` should match `text`.
   */
  public recordText(text: string, rev: Rev | null = null): Rev {
    // rev to edit from, and rev to match 'text'.
    const [aRev, bRev] = rev ? [rev, rev] : [this.maxRev, this.maxRev + 1];
    const b = text;

    const lines = splitLines(b);
    this.checkOut(aRev);
    const a = this.content;
    const blocks = diffLines(a, b);

    blocks.reverse().forEach(([a1, a2, b1, b2]) => {
      this.editChunk(a1, a2, bRev, lines.slice(b1, b2));
    });
    this.content = b;
    this.lastCheckoutKey = `${bRev},null`;
    if (bRev > this.maxRev) {
      this.maxRev = bRev;
    }

    // assert(this.reconstructContent() === b, "bug: text does not match");
    return bRev;
  }

  /** Get revision of the specified line. Returns null if out of range. */
  public getLineRev(i: LineIdx): Rev | null {
    if (i >= this.lines.length - 1) {
      return null;
    } else {
      return this.lines[i].rev;
    }
  }
}

/**
 * Calculate the differences.
 *
 * @param a Content of the "a" side.
 * @param b Content of the "b" side.
 * @returns A list of `(a1, a2, b1, b2)` tuples for the line ranges that
 * are different between "a" and "b".
 */
function diffLines(a: string, b: string): [LineIdx, LineIdx, LineIdx, LineIdx][] {
  const {chars1, chars2} = dmp.diff_linesToChars_(a, b);
  const blocks: [LineIdx, LineIdx, LineIdx, LineIdx][] = [];
  let a1 = 0,
    a2 = 0,
    b1 = 0,
    b2 = 0;
  const push = (len: number) => {
    if (a1 !== a2 || b1 !== b2) {
      blocks.push([a1, a2, b1, b2]);
    }
    a1 = a2 = a2 + len;
    b1 = b2 = b2 + len;
  };
  dmp.diff_main(chars1, chars2, false).forEach(x => {
    const [op, chars] = x;
    const len = chars.length;
    if (op === 0) {
      push(len);
    }
    if (op < 0) {
      a2 += len;
    }
    if (op > 0) {
      b2 += len;
    }
  });
  push(0);
  return blocks;
}

/**
 * Split lines by `\n`. Preserve the end of lines.
 */
function splitLines(s: string): string[] {
  let pos = 0;
  let nextPos = 0;
  const result = [];
  while (pos < s.length) {
    nextPos = s.indexOf('\n', pos);
    if (nextPos === -1) {
      nextPos = s.length - 1;
    }
    result.push(s.slice(pos, nextPos + 1));
    pos = nextPos + 1;
  }
  return result;
}

/** If the assertion fails, throw an `Error` with the given `message`. */
function assert(condition: boolean, message: string) {
  if (!condition) {
    throw new Error(message);
  }
}

export {LineLog};