RELEASE_TYPE: minor

This release makes stateful (rule-based) testing a first-class part of the Hegel protocol. Previously each library drove its own stateful tests, including choosing which rule to run next. The server now owns rule selection: a client registers its rules and invariants with the new `new_state_machine` command, then asks which rule to run at each step with `next_rule`.

Centralising selection lets the server bias it with swarm testing ([#126](https://github.com/hegeldev/hegel-core/issues/126)): for each test case a random subset of rules is disabled, so test cases exercise rules in more varied combinations. This surfaces bugs that uniform rule selection reaches only rarely — such as one that requires the same rule to run many times in a row.
