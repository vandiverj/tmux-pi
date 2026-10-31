---
id: kn-2026-10-31-test-cleanup-shared-tables
kind: convention
title: "Test cleanup"
scope: ["apps/launchpad/api/src/**/*.test.ts"]
tags: [test-isolation, drizzle]
versions: {drizzle-orm: ">=0.45 <0.46"}   # optional
source: review:review-coder-11a38097c8-032051
evidence: ["apps/launchpad/api/src/foo.test.ts:41", "review:review-coder-11a38097c8-032051#F2"]
supersedes: []
status: approved
created: 2026-10-31
last_reviewed: 2026-10-31
---
Tests under the API source must not truncate shared tables. Use the per-test schema helper instead.
