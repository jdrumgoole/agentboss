# How it works

## Where the data comes from

```text
~/.claude/
  sessions/<pid>.json                     live registry: pid, status, cwd, name
  projects/<encoded-cwd>/
    <session-id>.jsonl                    main transcript
    <session-id>/subagents/
      agent-<id>.jsonl                    subagent transcript
      agent-<id>.meta.json                agent type, description, spawn depth
  tasks/<session-id>/<n>.json             task-list items
```

## Incremental parsing

Transcripts are append-only and can reach tens of megabytes. Each file gets an
`IncrementalTranscript` that remembers a byte offset and a running aggregate,
so a rescan only parses bytes appended since the last look. A truncated or
replaced file resets the offset and is read again. Only complete lines are
consumed, because the tail of a live transcript is often a partial record.

## One turn, several records

One API turn is written as several `assistant` records, one per content block
(thinking, text, tool use), and every one repeats the same `message.usage`.
Counting each record would multiply tokens by the number of blocks, so usage is
credited once per `message.id`. Tool calls really are per record, and are
counted per record.

## Liveness

A registry entry outlives a crash. Liveness is not "the pid exists", because
pids get recycled: the pid must also still be a Claude process, checked against
the command name `ps` reports. The recorded `procStart` string cannot serve
here, since `ps -o lstart` formats it per locale and the two clocks disagree by
an hour across a daylight-saving boundary.

Liveness is never decided by a clock. The CLI only rewrites its registry file
when its status changes, so a quiet `updatedAt` means "still on one long tool
call", not "gone".
