#!/usr/bin/env bash
# wait-for.sh — wait for a shell condition to become true, then exit.
#
# The sandbox blocks foreground `sleep` in a poll loop (e.g. `sleep 300;
# tail log.txt`) because it burns a whole turn doing nothing observable.
# This is the sanctioned replacement: an agent watching a background job
# writes one call instead of a blocked sleep+check loop.
#
#   bin/wait-for.sh "grep -q DONE /tmp/harvest.log"
#   bin/wait-for.sh "test -f /tmp/out.json" --timeout 600 --interval 10
#
# The CONDITION is any shell command; success (exit 0) ends the wait. On
# success, if --tail names a readable file, its tail is printed for
# convenience. Exits 0 when the condition is met, 124 on timeout.
set -uo pipefail

usage() {
  echo "usage: wait-for.sh \"<condition-cmd>\" [--timeout SECONDS] [--interval SECONDS] [--tail FILE]" >&2
  echo "       wait-for.sh --sleep SECONDS" >&2
  exit 2
}

[ $# -ge 1 ] || usage

# --sleep N: a plain timed delay, for when there is genuinely nothing to poll
# (e.g. "give the deploy 5 minutes, then check once"). Foreground `sleep` is
# sandbox-blocked, so an agent reaching for it loses a turn to the rejection;
# this is the permitted equivalent, and is what a PreToolUse hook can rewrite
# a blocked `sleep N; <cmd>` into. Prefer a real condition when one exists —
# a condition finishes as soon as it is true, a delay always burns the full N.
if [ "$1" = "--sleep" ]; then
  DELAY="${2:?--sleep needs a value}"
  start=$(date +%s)
  while true; do
    elapsed=$(( $(date +%s) - start ))
    [ "$elapsed" -ge "$DELAY" ] && break
    remaining=$(( DELAY - elapsed ))
    # Cap each nap at 5s so a long delay can still be interrupted/observed
    # in reasonable increments, but never oversleep a short one.
    [ "$remaining" -gt 5 ] && remaining=5
    sleep "$remaining"
  done
  exit 0
fi

CONDITION="$1"; shift

TIMEOUT=300
INTERVAL=5
TAIL_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)  TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
    --interval) INTERVAL="${2:?--interval needs a value}"; shift 2 ;;
    --tail)     TAIL_FILE="${2:?--tail needs a value}"; shift 2 ;;
    *) echo "wait-for.sh: unknown arg '$1'" >&2; usage ;;
  esac
done

# pgrep -f self-match guard.
#
# The CONDITION string sits in this script's own argv, and in the argv of
# the `bash -c` that launched it. So a condition like
#   ! pgrep -f 'some-long-running-job'
# can have `pgrep -f` match wait-for.sh itself, forever — the wait can never
# end no matter what the real process does. Same `-f` self-match trap that
# any `pkill -f` on the kill side has to guard against too.
#
# The condition is eval'd in this shell, so a function named `pgrep` shadows
# the binary for it. It drops any match that is in this process's own family
# tree — both directions:
#
#   ancestors   — wait-for.sh itself, the `timeout`/`bash -c` that launched
#                 it, the agent harness above that. All carry the pattern in
#                 argv.
#   descendants — every `$(...)` command substitution bash forks here starts
#                 as a copy of wait-for.sh, argv and all, before it execs.
#                 Those transient subshells match too, and filtering
#                 ancestors alone does not catch them.
#
# Only the default PID-listing output is filtered — `pgrep -c` (count) and
# `pgrep -d` (custom delimiter) are passed through unfiltered and will still
# self-match. Use a real condition, not a count, when it matters.
ppid_of() {   # read ppid from /proc rather than forking `ps` — a fork here is
              # itself a self-matching process, which is the bug we are fixing
  local stat
  [ -r "/proc/$1/stat" ] || return 1
  stat=$(< "/proc/$1/stat")
  stat=${stat#*) }          # skip pid and the (comm) field, which may contain spaces
  set -- $stat
  printf '%s\n' "$2"        # state, ppid
}

pgrep() {
  local kin p out pid keep filtered
  kin=" "
  p=$$
  while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
    kin="$kin$p "
    p=$(ppid_of "$p") || break
  done

  out=$(command pgrep "$@" 2>/dev/null) || return 1
  [ -n "$out" ] || return 1

  case " $* " in
    *" -c "*|*" -d "*) printf '%s\n' "$out"; return 0 ;;
  esac

  local selfcmd
  selfcmd=$(tr '\0' ' ' < "/proc/$$/cmdline" 2>/dev/null)

  filtered=""
  for pid in $out; do
    case "$kin" in *" $pid "*) continue ;; esac   # self or ancestor

    # Already gone. Every `$(...)` fork here is briefly a copy of wait-for.sh
    # and shows up in our own pgrep output before it execs or exits, so a
    # candidate that has vanished by the time we classify it is ours, not the
    # process being waited on. Failing open here is what keeps the guard from
    # being defeated by that timing window.
    [ -r "/proc/$pid/stat" ] || continue

    # A fork of this very script: same argv, different pid, not yet exec'd.
    [ "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" = "$selfcmd" ] && continue

    keep=1                                        # walk up: descendant of self?
    p=$pid
    while [ -n "$p" ] && [ "$p" != "0" ] && [ "$p" != "1" ]; do
      p=$(ppid_of "$p") || break
      [ "$p" = "$$" ] && { keep=0; break; }
    done
    [ "$keep" = 1 ] && filtered="$filtered$pid
"
  done

  [ -n "$filtered" ] || return 1
  printf '%s' "$filtered"
}

start=$(date +%s)
while true; do
  if eval "$CONDITION" >/dev/null 2>&1; then
    echo "wait-for: condition met after $(( $(date +%s) - start ))s: $CONDITION"
    [ -n "$TAIL_FILE" ] && [ -r "$TAIL_FILE" ] && { echo "--- tail $TAIL_FILE ---"; tail -n 20 "$TAIL_FILE"; }
    exit 0
  fi
  if [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ]; then
    echo "wait-for: TIMEOUT after ${TIMEOUT}s waiting for: $CONDITION" >&2
    [ -n "$TAIL_FILE" ] && [ -r "$TAIL_FILE" ] && { echo "--- tail $TAIL_FILE ---" >&2; tail -n 20 "$TAIL_FILE" >&2; }
    exit 124
  fi
  sleep "$INTERVAL"
done
