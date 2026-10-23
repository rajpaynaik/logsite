# The README's demo GIF. From the repo root:
#   python bench/repro/make_incidents.py --scenario hard-noisy-day
#   vhs docs/demo/demo.tape
Output docs/demo/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1460
Set Height 660
Set Padding 18
Set Theme "Dracula"
Set TypingSpeed 22ms
Set CursorBlink false

Hide
Type "export XDG_CACHE_HOME=/tmp/logsite-demo PATH=$PWD/.venv/bin:$PATH PS1='$ ' && D=$(mktemp -d) && cp -r examples/bookstore $D/ && cp bench/repro/scenarios/hard-noisy-day/incident.log $D/bookstore/ && cd $D/bookstore && clear"
Enter
Sleep 1s
Show

Type "wc -l incident.log && cut -c1-96 incident.log | sed -n '20000,20022p'"
Enter
Sleep 2.2s
Type "clear && logsite errors --from incident.log --brief"
Enter
Sleep 3.5s
Type "clear && logsite repro --from incident.log --write-test"
Enter
Sleep 5s
Type "clear && logsite agent --expect 9aaf88c8 -- python -m pytest tests/test_repro_9aaf88c8.py"
Enter
Sleep 4s
