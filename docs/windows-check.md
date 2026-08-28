# The Windows check

Ten minutes on a real Windows machine, once per release. CI proves the
installer, the daemon's Windows branches and the real Claude Code CLI there;
it cannot prove the two things a customer hits first, because no runner has a
signed-in Claude Desktop or a board on a COM port.

**Needs:** a Windows 10/11 PC or VM with USB passthrough; Claude Desktop
installed and signed in; Claude Code installed (`npm install -g
@anthropic-ai/claude-code`); a BLINK on a USB cable.

Download `blink-windows-latest` from the latest CI run's artifacts (Actions ->
the run -> Artifacts), or the release's `blink-windows-x86_64.exe`.

| # | Do | Expect |
|---|---|---|
| 1 | Open Claude Desktop, use it once, close it. | -- |
| 2 | In PowerShell: `dir $env:APPDATA\Claude\plan-usage-history.json` | **The file exists.** If it does not, the Desktop path assumption is wrong: find the file (`dir -Recurse $env:APPDATA,$env:LOCALAPPDATA -Filter plan-usage-history.json`) and report the real path. |
| 3 | `.\blink.exe` | The disclosure, then four steps ending `[4/4] Background service ... running`. No stack trace. |
| 4 | `.\blink.exe status` | `Bridge registered as a Scheduled Task`, `Claude Code <version>`, `Activity hooks installed (10/10 events)`, `Desktop usage cache parsed, reading N min old`. **If it says `looked at ...`, step 2's path is not the one it checked -- report both.** |
| 5 | Open a terminal, run `claude`, ask it something, wait for the reply. Then `.\blink.exe status` again. | `Usage data fresh`, `1 live session`. |
| 6 | `.\blink.exe status --wire` | One JSON line with `session_pct`, `weekly_pct`, `provider":"claude"`, `src":"cli"`, `state`. |
| 7 | Plug the board in. Device Manager -> Ports: a `USB-SERIAL CH340 (COMn)` entry. | If the entry shows a warning, Windows has no CH340 driver: note the Windows version -- that is a README item. |
| 8 | Within 60 s the panel shows the numbers from step 6. | Boot clip, then the gauges. |
| 9 | Start a Claude Code turn; watch the pip. Leave the terminal idle at its prompt for 4 minutes. | Pip pulses while it works, goes steady when done, **does not turn red** while idle. |
| 10 | Unplug and replug the board. | Panel back within a minute, no reboot loop, no reset of the board each time (`%USERPROFILE%\.blink\bridge.log` says `answered; not resetting it`). |
| 11 | `.\blink.exe uninstall` | Status line and hooks gone from `%USERPROFILE%\.claude\settings.json`, task gone (`schtasks /query /tn "Blink bridge"` fails), `%USERPROFILE%\.blink\bin` gone a moment after the window closes. |

Paste the outputs of steps 2, 4, 6 and 10 into the release notes. Anything
unexpected: the `bridge.log` and the output of `status --wire` are what a bug
report needs.
