#!/usr/bin/env python3
"""Build PowerShell payloads for the Windows MT5 box (run them with scripts/box/ssm_run.sh).

The box has no git: each bridge is a COPIED source tree with an editable install
(`pip install -e .` into its own .venv), so updating a bridge means overwriting files
under its src/ and restarting its scheduled task.

  python scripts/box/gen_payloads.py discover --baseline <git-rev> --out <dir>
      Read-only. For every bridge folder: hashes of every src/**/*.py compared with
      <git-rev>, CRLF counts, install mode, package versions, non-secret .env keys;
      plus python processes -> listening ports, and the MCP scheduled tasks.

  python scripts/box/gen_payloads.py deploy --folder <name> --baseline <git-rev> --out <dir> FILE...
      Writes deploy / rollback payloads for one folder. Each FILE (repo-relative) is
      embedded as base64; on the box it is written only if its current hash equals the
      baseline hash (else SKIP), backed up to <file>.bak-<stamp>, then re-hashed.
      A file already at the target hash reports NOOP, so a re-run is harmless.
"""
import argparse
import base64
import hashlib
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
PROJECTS = r"C:\Users\Administrator\projects"
FOLDERS = {
	# folder name -> what it serves (from the 2026-08-19 discovery; verify with `discover`)
	# (.env SERVER as of 2026-11-02)
	"metatrader-mcp-server-demo": "8080, terminal #1, VTMarkets-Demo",
	"metatrader-mcp-server-prod": "8081, terminal #2 (C:\\MT5-Prod), VTMarkets-Live 6",
	"metatrader-mcp-server-seagull": "8082, terminal #3 (C:\\MT5-Seagull), VTMarkets-Live 6",
}
# .env keys that are safe to print (never LOGIN / PASSWORD).
SAFE_ENV_KEYS = ["MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MT5_PATH", "MT5_PORTABLE", "SERVER"]


def sha(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest().upper()


def git_bytes(rev: str, path: str) -> bytes:
	return subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:{path}"], check=True, capture_output=True).stdout


def git_py_files(rev: str) -> list[str]:
	out = subprocess.run(
		["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", rev, "src"], check=True, capture_output=True, text=True
	).stdout
	return [p for p in out.splitlines() if p.endswith(".py")]


def win(path: str) -> str:
	return path.replace("/", "\\")


def ps_quote(s: str) -> str:
	return "'" + s.replace("'", "''") + "'"


def discover(rev: str) -> str:
	manifest = {win(p): sha(git_bytes(rev, p)) for p in git_py_files(rev)}
	lines = [
		"$ErrorActionPreference = 'Continue'",
		f"$baselineRev = {ps_quote(rev)}",
		"$manifest = @{",
		*[f"  {ps_quote(k)} = {ps_quote(v)}" for k, v in sorted(manifest.items())],
		"}",
		"$folders = @(" + ", ".join(ps_quote(f) for f in FOLDERS) + ")",
		"$safeKeys = @(" + ", ".join(ps_quote(k) for k in SAFE_ENV_KEYS) + ")",
		f"$root = {ps_quote(PROJECTS)}",
		r"""
foreach ($f in $folders) {
  $proj = Join-Path $root $f
  Write-Output ""
  Write-Output "################ $f ################"
  if (-not (Test-Path $proj)) { Write-Output "MISSING $proj"; continue }
  Write-Output ("has .git = " + (Test-Path (Join-Path $proj '.git')))

  Write-Output "=== src/**/*.py vs $baselineRev ==="
  $same = 0; $diff = @(); $extra = @(); $crlf = @()
  $onBox = Get-ChildItem -Path (Join-Path $proj 'src') -Recurse -Filter *.py -File |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\' }
  foreach ($file in $onBox) {
    $rel = $file.FullName.Substring($proj.Length + 1)
    $h = (Get-FileHash -Algorithm SHA256 $file.FullName).Hash
    $bytes = [IO.File]::ReadAllBytes($file.FullName)
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Contains("`r`n")) { $crlf += $rel }
    if (-not $manifest.ContainsKey($rel)) { $extra += "$rel sha256:$($h.Substring(0,16))" }
    elseif ($manifest[$rel] -eq $h) { $same++ }
    else { $diff += "$rel box:$($h.Substring(0,16)) rev:$($manifest[$rel].Substring(0,16))" }
  }
  $missing = $manifest.Keys | Where-Object { -not (Test-Path (Join-Path $proj $_)) }
  Write-Output "identical: $same of $($manifest.Count)"
  foreach ($d in $diff)    { Write-Output "DIFF    $d" }
  foreach ($m in $missing) { Write-Output "MISSING $m" }
  foreach ($e in $extra)   { Write-Output "EXTRA   $e" }
  foreach ($c in $crlf)    { Write-Output "CRLF    $c" }

  Write-Output "=== install / versions ==="
  $py = Join-Path $proj '.venv\Scripts\python.exe'
  if (Test-Path $py) {
    & $py -c "import sys, importlib.metadata as m; print('python', sys.version.split()[0]); [print(n, (m.version(n) if any(d.metadata['Name'].lower() == n.lower() for d in m.distributions()) else 'NOT INSTALLED')) for n in ('mcp','MetaTrader5','metatrader-mcp-server','uvicorn','starlette')]" 2>&1
    Get-ChildItem (Join-Path $proj '.venv\Lib\site-packages') -Filter '__editable__*metatrader*' | ForEach-Object { "editable: $($_.Name)" }
  } else { Write-Output "NO VENV PYTHON at $py" }

  Write-Output "=== .env (safe keys only) ==="
  $envFile = Join-Path $proj '.env'
  if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $line = $_; $safeKeys | Where-Object { $line -match "^\s*$_\s*=" } }
  } else { Write-Output "no .env" }
}

Write-Output ""
Write-Output "################ processes ################"
$listen = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | ForEach-Object {
  $p = $_
  $ports = ($listen | Where-Object { $_.OwningProcess -eq $p.ProcessId } | ForEach-Object { $_.LocalPort } | Sort-Object -Unique) -join ','
  Write-Output "PID $($p.ProcessId) PARENT $($p.ParentProcessId) PORTS [$ports] STARTED $($p.CreationDate)"
  Write-Output "    EXE: $($p.ExecutablePath)"
  Write-Output "    CMD: $($p.CommandLine)"
}

Write-Output ""
Write-Output "################ scheduled tasks ################"
Get-ScheduledTask | Where-Object { $_.TaskName -match 'MCP' } | ForEach-Object {
  $t = $_
  $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath
  Write-Output "TASK $($t.TaskPath)$($t.TaskName)  state=$($t.State)  lastRun=$($info.LastRunTime)  lastResult=$($info.LastTaskResult)"
  foreach ($a in $t.Actions) { Write-Output "    RUN: $($a.Execute) $($a.Arguments)  (in: $($a.WorkingDirectory))" }
  foreach ($tr in $t.Triggers) { Write-Output "    TRIGGER: $($tr.CimClass.CimClassName)" }
  Write-Output "    USER: $($t.Principal.UserId)  runLevel=$($t.Principal.RunLevel)"
}
# .bat files can carry credentials on the command line: print them with every
# --password/--login value and any PASSWORD=/LOGIN= assignment masked.
Get-ChildItem 'C:\Users\Administrator\*.bat' -ErrorAction SilentlyContinue | ForEach-Object {
  Write-Output "BAT $($_.FullName)"
  Get-Content $_.FullName | ForEach-Object {
    $l = $_ -replace '(?i)(--(password|login)[ =])("[^"]*"|\S+)', '$1***'
    $l = $l -replace '(?i)((password|login)\s*=\s*)\S+', '$1***'
    "    $l"
  }
}
Write-Output ""
Write-Output "=== DISCOVERY DONE ==="
""",
	]
	return "\n".join(lines)


def deploy(folder: str, rev: str, files: list[str]) -> tuple[str, str]:
	stamp = time.strftime("%Y%m%d-%H%M")
	proj = f"{PROJECTS}\\{folder}"
	entries = []
	for f in files:
		new = (REPO / f).read_bytes()
		if b"\r\n" in new:
			sys.exit(f"{f} has CRLF; the box copies are LF")
		entries.append((win(f), sha(git_bytes(rev, f)), sha(new), base64.b64encode(new).decode()))

	head = [
		"$ErrorActionPreference = 'Stop'",
		f"$proj = {ps_quote(proj)}",
		f"$stamp = {ps_quote(stamp)}",
		"if (-not (Test-Path $proj)) { Write-Output \"FAIL no such folder $proj\"; exit 1 }",
		"$files = @(",
		*[
			f"  @{{ rel = {ps_quote(r)}; old = {ps_quote(o)}; new = {ps_quote(n)}; b64 = {ps_quote(b)} }}"
			for r, o, n, b in entries
		],
		")",
	]
	deploy_body = r"""
Write-Output "=== DEPLOY to $proj ==="
# Gate first: refuse to write anything unless every file is at the baseline or already at the target.
$blocked = $false
foreach ($f in $files) {
  $path = Join-Path $proj $f.rel
  $cur = if (Test-Path $path) { (Get-FileHash -Algorithm SHA256 $path).Hash } else { 'MISSING' }
  if ($cur -ne $f.old -and $cur -ne $f.new) {
    Write-Output "SKIP $($f.rel): box $($cur.Substring(0,16)) is neither baseline $($f.old.Substring(0,16)) nor target $($f.new.Substring(0,16))"
    $blocked = $true
  }
}
if ($blocked) { Write-Output "=== RESULT: nothing written (baseline mismatch) ==="; exit 1 }
foreach ($f in $files) {
  $path = Join-Path $proj $f.rel
  $cur = (Get-FileHash -Algorithm SHA256 $path).Hash
  if ($cur -eq $f.new) { Write-Output "NOOP $($f.rel): already at target"; continue }
  Copy-Item $path "$path.bak-$stamp"
  [IO.File]::WriteAllBytes($path, [Convert]::FromBase64String($f.b64))
  $after = (Get-FileHash -Algorithm SHA256 $path).Hash
  if ($after -ne $f.new) { Write-Output "FAIL $($f.rel): wrote $($after.Substring(0,16)), expected $($f.new.Substring(0,16))"; exit 1 }
  Write-Output "OK   $($f.rel): $($cur.Substring(0,16)) -> $($after.Substring(0,16))  (backup .bak-$stamp)"
}
Write-Output "=== compile check ==="
$py = Join-Path $proj '.venv\Scripts\python.exe'
Get-ChildItem -Path (Join-Path $proj 'src') -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
foreach ($f in $files) {
  & $py -m py_compile (Join-Path $proj $f.rel) 2>&1
  if ($LASTEXITCODE -eq 0) { Write-Output "COMPILES $($f.rel)" } else { Write-Output "SYNTAX ERROR $($f.rel) -- roll back, do not restart" }
}
Write-Output "=== import check (separate process; does not touch the terminal) ==="
Push-Location $proj
& $py -c "import metatrader_mcp.server as s, metatrader_mcp.utils as u; app = u.build_dual_app(s.mcp); print('IMPORT OK', len(s.mcp._tool_manager.list_tools()), 'tools; routes', sorted({getattr(r, 'path', '') for r in app.routes}))" 2>&1
if ($LASTEXITCODE -ne 0) { Write-Output "IMPORT FAILED -- roll back, do not restart" }
Pop-Location
Write-Output "=== RESULT: files deployed. Restart this folder's bridge task to load them. ==="
"""
	rollback_body = r"""
Write-Output "=== ROLLBACK in $proj ==="
foreach ($f in $files) {
  $path = Join-Path $proj $f.rel
  $bak = Get-ChildItem "$path.bak-*" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
  if (-not $bak) { Write-Output "NONE $($f.rel): no backup"; continue }
  Copy-Item $bak.FullName $path -Force
  $h = (Get-FileHash -Algorithm SHA256 $path).Hash
  $tag = if ($h -eq $f.old) { 'baseline' } else { 'NOT baseline' }
  Write-Output "RESTORED $($f.rel) from $($bak.Name): $($h.Substring(0,16)) ($tag)"
}
Get-ChildItem -Path (Join-Path $proj 'src') -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Write-Output "=== RESULT: rolled back. Restart this folder's bridge task. ==="
"""
	# The rollback payload needs only the hashes, not the file bodies.
	rb_head = [line if "b64 =" not in line else line.split("; b64 =")[0] + " }" for line in head]
	return "\n".join(head) + deploy_body, "\n".join(rb_head) + rollback_body


def align(source: str, target: str) -> str:
	"""Make <target>'s venv match <source>'s exact package set (pip freeze, editable excluded).

	Must run while the target bridge is STOPPED: Windows locks loaded .pyd files (e.g.
	MetaTrader5's), so pip cannot replace them under a running process. The payload
	refuses to touch anything if a python from the target folder is running.
	"""
	return "\n".join([
		"$ErrorActionPreference = 'Continue'",
		f"$src = {ps_quote(PROJECTS + chr(92) + source)}",
		f"$dst = {ps_quote(PROJECTS + chr(92) + target)}",
		r"""
$srcPy = Join-Path $src '.venv\Scripts\python.exe'
$dstPy = Join-Path $dst '.venv\Scripts\python.exe'
Write-Output "=== ALIGN $dst  <-  $src ==="
foreach ($p in $srcPy, $dstPy) { if (-not (Test-Path $p)) { Write-Output "FAIL no python at $p"; exit 1 } }

# Refuse while the target bridge runs: its venv launcher and the child it spawns.
$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
  $_.ExecutablePath -like "$dst\*" -or
  ($_.ParentProcessId -and (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.ParentProcessId)").ExecutablePath -like "$dst\*")
}
if ($running) {
  Write-Output "FAIL the $dst bridge is running (PIDs $(($running | ForEach-Object ProcessId) -join ', ')). Stop its task first."
  exit 1
}

& $srcPy -m pip --version
if ($LASTEXITCODE -ne 0) { Write-Output "FAIL no pip in source venv"; exit 1 }
& $dstPy -m pip --version
if ($LASTEXITCODE -ne 0) { Write-Output "FAIL no pip in target venv"; exit 1 }

$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$req    = Join-Path $env:TEMP "align-src-$stamp.txt"
$before = Join-Path $dst ".venv\freeze-before-align-$stamp.txt"
& $srcPy -m pip freeze --exclude-editable | Set-Content -Encoding ascii $req
& $dstPy -m pip freeze --exclude-editable | Set-Content -Encoding ascii $before
Write-Output "target freeze saved to $before (rollback: pip install -r it)"

Write-Output "=== differences before ==="
Compare-Object (Get-Content $before) (Get-Content $req) | ForEach-Object {
  $side = if ($_.SideIndicator -eq '=>') { 'source' } else { 'target' }
  "  $side  $($_.InputObject)"
}

Write-Output "=== pip install -r (source pins) ==="
& $dstPy -m pip install --disable-pip-version-check -r $req 2>&1 | Where-Object { $_ -notmatch '^Requirement already satisfied' }
if ($LASTEXITCODE -ne 0) { Write-Output "FAIL pip install exited $LASTEXITCODE"; exit 1 }

Write-Output "=== differences after (only target-only extras may remain) ==="
$after = & $dstPy -m pip freeze --exclude-editable
$left = Compare-Object $after (Get-Content $req)
if ($left) { $left | ForEach-Object { "  $($_.SideIndicator)  $($_.InputObject)" } } else { Write-Output "  none: identical" }
& $dstPy -c "import importlib.metadata as m; [print(n, m.version(n)) for n in ('mcp','MetaTrader5','uvicorn','starlette','pydantic','pydantic-settings')]"
& $dstPy -c "import MetaTrader5, metatrader_mcp.server; print('IMPORT OK')"
Write-Output "=== RESULT: aligned. Start the bridge task again. ==="
""",
	])


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = ap.add_subparsers(dest="cmd", required=True)
	d = sub.add_parser("discover")
	d.add_argument("--baseline", required=True)
	d.add_argument("--out", required=True, type=pathlib.Path)
	p = sub.add_parser("deploy")
	p.add_argument("--folder", required=True, choices=sorted(FOLDERS))
	p.add_argument("--baseline", required=True)
	p.add_argument("--out", required=True, type=pathlib.Path)
	p.add_argument("files", nargs="+")
	al = sub.add_parser("align")
	al.add_argument("--source", required=True, choices=sorted(FOLDERS))
	al.add_argument("--target", required=True, choices=sorted(FOLDERS))
	al.add_argument("--out", required=True, type=pathlib.Path)
	a = ap.parse_args()
	a.out.mkdir(parents=True, exist_ok=True)

	if a.cmd == "align":
		short = a.target.removeprefix("metatrader-mcp-server-")
		out = a.out / f"box_{short}_align.ps1"
		out.write_text(align(a.source, a.target), encoding="utf-8", newline="\n")
		print(out)
	elif a.cmd == "discover":
		out = a.out / "box_discover.ps1"
		out.write_text(discover(a.baseline), encoding="utf-8", newline="\n")
		print(out)
	else:
		short = a.folder.removeprefix("metatrader-mcp-server-")
		dep, rb = deploy(a.folder, a.baseline, a.files)
		for name, body in ((f"box_{short}_deploy.ps1", dep), (f"box_{short}_rollback.ps1", rb)):
			(a.out / name).write_text(body, encoding="utf-8", newline="\n")
			print(a.out / name)


if __name__ == "__main__":
	main()
