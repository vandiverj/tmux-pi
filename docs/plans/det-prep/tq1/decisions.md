# TQ1 owner decisions (2026-11-11)

Owner: "all as recommended" on the S1-S5 reports. Every change keeps every assertion; covered lines the same or larger; time and
count before and after; cross-provider review.

| Lane | Items | Worker | When |
|---|---|---|---|
| Q1 quick fixes | S1(a) test_13 limit test; S3(a) worker_files `:420` poll clock; S5(b) retry-gate rows stop after the gate | Sonnet | now |
| Q2 input sizes | S2(a) sensitive-read reruns keep decision rows; S2(b) smaller oversized inputs; S4(b) oracle/linear inputs shrunk | Sonnet | now |
| Q3 folds | S1(b) identical-coverage groups to table-driven tests; S5(c) review_rounds groups | Sonnet | after Q1 merges |
| Q4 shared setup | S1(c) lanes; S3(b) ~19 full-run tests; S5(a) TpReviewTests | Sonnet (Opus if the design needs it) | last |
| Main | S4(a) measure one G3 fake run | Main | now |

Not proposed: no deletions. Tests that alone hold coverage are kept as listed in each report.
