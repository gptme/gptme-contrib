/**
 * Per-key serial queue.
 *
 * Tasks sharing a key run one at a time, in submission order; tasks with
 * different keys run concurrently.
 *
 * The bridge needs this because a sender has exactly one agent session:
 * `sessionArgs` is a check-then-act on the session file, so two concurrent
 * calls both see "no session yet" and both try `--session-id` (the second is
 * rejected, losing that reply), and two concurrent `--resume` runs append to
 * the same conversation and interleave history.
 */

/** Create a queue that serializes tasks per key. */
export function createKeyedQueue() {
    const tails = new Map();

    /**
     * Run `task` after every earlier task for `key` has settled.
     * Returns the task's own promise, so callers see its result or rejection.
     */
    function run(key, task) {
        const previous = tails.get(key) ?? Promise.resolve();
        // The previous tail never rejects, so a failed task cannot block its
        // successors; each caller still gets its own result.
        const result = previous.then(() => task());
        const tail = result.catch(() => {});
        tails.set(key, tail);
        tail.then(() => {
            if (tails.get(key) === tail) {
                tails.delete(key);
            }
        });
        return result;
    }

    /** Number of keys with pending work (for tests and leak checks). */
    function size() {
        return tails.size;
    }

    return { run, size };
}
