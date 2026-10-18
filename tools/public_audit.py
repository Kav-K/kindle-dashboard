"""Fail closed on credential files, private keys, leaked tokens and unsafe sources.

Checks tracked Git files when run from a checkout; otherwise checks the source
tree, excluding generated build/runtime directories. Reports paths and rule
names only, never matched contents.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import subprocess
import sys

SKIP={'.git','.venv','build','dist','out','kindle-out','device-package','__pycache__','.pytest_cache'}
BAD_SUFFIX={'.key','.p12','.pfx','.sqlite','.sqlite3','.db','.zip','.7z','.log'}
BAD_NAMES={'secrets.json','.env','authorized_keys','known_hosts','id_rsa','id_ed25519'}
RULES={
 'private-key':re.compile(r'-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----'),
 'github-token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
 'provider-token':re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{35,}\b'),
 'aws-key':re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
 'credential-url':re.compile(r'https?://([^\s/\"\'<>:@]+):([^\s/\"\'<>@]+)@([A-Za-z0-9.-]+)',re.I),
 'windows-personal-path':re.compile(r'[A-Za-z]:[\\/]Users[\\/](?!Public[\\/]|runneradmin[\\/])[^\s\"\'<>]+',re.I),
}

def source_files(root: Path):
    if (root/'.git').exists():
        top=subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True,check=True,timeout=10)
        if Path(top.stdout.strip()).resolve()!=root.resolve():raise ValueError('Unexpected Git root')
        result=subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,check=True,timeout=10)
        return [root/name.decode('utf-8') for name in result.stdout.split(b'\0') if name]
    return [p for p in root.rglob('*') if not any(part in SKIP or part.startswith('.venv-') or part.endswith('.egg-info') for part in p.relative_to(root).parts) and (p.is_file() or p.is_symlink())]

def audit(root: Path, staged: bool = False):
    issues=[];count=0
    if staged:
        if not (root/'.git').exists():return 0,[('.', 'staged-gate-requires-checkout')]
        result=subprocess.run(['git','-C',str(root),'diff','--quiet','--no-ext-diff','--'],capture_output=True,timeout=10)
        # A clean worktree/index comparison guarantees the inspected tracked
        # contents also describe what would be committed. Do not let a clean
        # worktree conceal a different staged blob.
        if result.returncode!=0:return 0,[('.', 'index-worktree-mismatch')]
    for path in source_files(root):
        rel=path.relative_to(root).as_posix()
        if path.is_symlink():issues.append((rel,'symlink'));continue
        if not path.is_file():issues.append((rel,'missing-tracked-file'));continue
        count+=1
        if path.name in BAD_NAMES or path.name.endswith('.credential.xml') or path.suffix.lower() in BAD_SUFFIX:
            issues.append((rel,'private-or-generated-file'))
        if any(part in {'.private','.inkboard','inkboard-data','runtime'} for part in path.relative_to(root).parts):
            issues.append((rel,'runtime-directory'))
        raw=path.read_bytes()
        if len(raw)>2_000_000:issues.append((rel,'oversized-source'));continue
        try:text=raw.decode('utf-8')
        except UnicodeError:issues.append((rel,'unexpected-binary'));continue
        for name,rule in RULES.items():
            matches=list(rule.finditer(text))
            if name=='credential-url':
                # Explicitly fake credentials on IANA-reserved example domains
                # are used to test rejection. Do not exempt the entire fixture.
                matches=[m for m in matches if not (m[1]=='user' and m[2]=='pass' and
                    any(m[3]==host or m[3].endswith('.'+host) for host in ('example.com','example.net','example.org','example','invalid','test')))]
            if matches:issues.append((rel,name))
    return count,issues

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--staged',action='store_true',help='Also require inspected tracked files to match the Git index')
    args=parser.parse_args()
    try:count,issues=audit(args.root.resolve(),args.staged)
    except (OSError,ValueError,subprocess.SubprocessError):
        print('Publication audit could not inspect the source safely.',file=sys.stderr);return 1
    for path,rule in issues:print(f'{path}: {rule}',file=sys.stderr)
    if issues:return 1
    print(f'Publication audit passed: {count} source files; no credential material detected.')
    return 0

if __name__=='__main__':raise SystemExit(main())
