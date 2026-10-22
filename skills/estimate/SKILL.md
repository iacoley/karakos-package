---
name: estimate
description: Estimate agent time and spend for a piece of work before starting it, then compare with what it actually cost
---

An estimate is a promise you can check. Make it before you start, write it down,
and compare it afterwards. Estimates that are never compared do not improve.

## 1. Split the work

List the builds, not the feature. A build is one commit-sized change you can test.
If you cannot name the files it touches, you do not know enough to estimate it yet.

## 2. Estimate each build in agent minutes

- Read-and-change in one file, with a test: about 10 to 15 minutes.
- A new module with tests and docs: about 20 to 30 minutes.
- Anything touching a protected path adds a review round trip. Do not count the
  wait as your time, but do say it in the estimate.
- Add a review-fix round for every build that goes through review. Two rounds is
  normal.

## 3. Estimate spend

Take a baseline from the admin tools before you start: `get_cost` (spend per agent)
and `get_usage` (rate-limit headroom). After the work, take them again. The difference
is the actual. If the build queue is enabled, `bin/buildq list` shows finished builds
for comparison.

## 4. Write it down and close the loop

Put the estimate where the owner can see it (the issue, or the task). When the work
ends, add one line: estimated, actual, and the reason for any gap over a third.
The next estimate starts from your own gaps, not from the table above.

## 5. Be honest about uncertainty

Give a range, not a point. Say what could blow it up (an unknown file, a missing
test fixture, a review that finds a design problem). Do not pad quietly.
