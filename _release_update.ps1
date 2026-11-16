$ErrorActionPreference = "Continue"
$env:Path = "C:\Program Files\Git\cmd;" + [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
$env:GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
$env:GIT_TERMINAL_PROMPT = "0"
Set-Location C:\Users\Akgun\projects\kvm-switcher

Remove-Item "_probe_update.py","_probe_update2.py" -Force -ErrorAction SilentlyContinue

Write-Host "=== bump to 0.2.0 ==="
$path = "$PWD\kvm-switcher.py"
$text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
$text = $text.Replace('VERSION = "0.1.6"', 'VERSION = "0.2.0"')
[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
(Select-String -Path $path -Pattern '^VERSION =').Line

Write-Host "=== tests ==="
python -m unittest discover -s tests 2>&1 | Select-Object -Last 3
Remove-Item "__pycache__","tests\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "=== help text shows new commands ==="
python kvm-switcher.py --help | Select-String -Pattern 'check|update'

Write-Host "=== commit ==="
git add -A
git status --short
$msg = @'
Add check and update commands

`check` compares the running version with the one published at the same
ref the installers fetch from and says whether an update exists. `update`
replaces the installed program with it.

update refuses to run inside a git checkout and says to use git pull, so
a working tree is never overwritten.

The download is verified before anything is replaced: it must start with
a version, be valid UTF-8, carry no BOM (which would break the shebang
install.sh symlinks onto PATH), compile, and be newer. The replacement is
written beside the target and swapped with os.replace so a failure cannot
leave a half-written program.

update only stops the watcher when it is running that same file, so a
copy installed elsewhere never disturbs another install's watcher. The
restarted watcher gets DEVNULL stdio: inheriting the caller's pipe made
`update` hang whenever its output was captured.

Bump VERSION to 0.2.0.
'@
[System.IO.File]::WriteAllText("$env:TEMP\msg7.txt", $msg, (New-Object System.Text.UTF8Encoding($false)))
git -c user.name="Algün Akgündüz" -c user.email="algun.t.akgunduz@gmail.com" commit -F "$env:TEMP\msg7.txt"
Write-Host "commit exit=$LASTEXITCODE"

git push git@github.com:algun/kvm-switcher.git main 2>&1
Write-Host "push exit=$LASTEXITCODE"
git -c user.name="Algün Akgündüz" -c user.email="algun.t.akgunduz@gmail.com" tag -a v0.2.0 -m "kvm-switcher 0.2.0"
git push git@github.com:algun/kvm-switcher.git v0.2.0 2>&1
Write-Host "tag exit=$LASTEXITCODE"
git log --oneline -2
Write-Host "RELEASE-DONE"
