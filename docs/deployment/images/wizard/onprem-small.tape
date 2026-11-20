# Regenerates the wizard screenshots in this directory: vhs docs/deployment/images/wizard/onprem-small.tape
# Run from the repo root. VHS (https://github.com/charmbracelet/vhs) needs ttyd and ffmpeg on PATH.

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 980
Set Padding 20
Set TypingSpeed 15ms
Set Theme "Builtin Dark"

Hide
Type "export PS1='$ ' && clear" Enter
Show

Type "python3 scripts/dfe-ops init --out deployment.yaml --force" Enter
Sleep 1s
Type "acme-dfe" Enter
Type "on-prem" Enter
Type "acme-prod" Enter
Enter
Sleep 500ms
Type "slim" Enter
Type "50" Enter
Enter
Enter
Sleep 500ms
Type "y" Enter
Type "n" Enter
Type "n" Enter
Type "n" Enter
Type "n" Enter
Type "n" Enter
Sleep 500ms
Type "y" Enter
Type "203.0.113.0/24" Enter
Enter
Type "dfe.example.com" Enter
Type "y" Enter
Enter
Sleep 1s
Screenshot docs/deployment/images/wizard/onprem-small.png
