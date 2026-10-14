#!/usr/bin/env python3
"""Final-byte DMG and its single installer action; no dependency download."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.core.agent_entry.component_manifest import verify_component_manifest
from src.core.agent_entry.delivery_identity import MANIFEST_NAME,verify_final_signature
from src.core.agent_entry.delivery_package import verify_delivery_dmg,_read

LAUNCHER_SOURCE=r'''
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(void) {
  char raw[PATH_MAX], path[PATH_MAX]; uint32_t size=sizeof(raw);
  if (_NSGetExecutablePath(raw,&size) || !realpath(raw,path)) return 70;
  for (int i=0;i<4;i++) { char *slash=strrchr(path,'/'); if (!slash) return 70; *slash=0; }
  const char *script="set -eu\n"
    "stage=$(/usr/bin/mktemp -d \"${TMPDIR:-/private/tmp/}lulu-installer.XXXXXXXX\")\n"
    "/usr/bin/ditto \"$1/Lulu.app\" \"$stage/Lulu.app\"\n"
    "/usr/bin/codesign --verify --strict \"$stage/Lulu.app\"\n"
    "/usr/bin/codesign --verify --deep --strict \"$stage/Lulu.app\"\n"
    "exec \"$stage/Lulu.app/Contents/MacOS/Lulu\" --install-lulu --source-volume \"$1\"\n";
  execl("/bin/sh","sh","-c",script,"lulu-installer",path,(char*)0);
  return 70;
}
'''


def create_installer(destination):
    app=Path(destination);macos=app/'Contents/MacOS';macos.mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'InstallLulu',
        'CFBundleIdentifier':'com.lulu.transcribe.installer','CFBundleName':'安装 Lulu',
        'CFBundleDisplayName':'安装 Lulu','CFBundlePackageType':'APPL','LSUIElement':False}))
    with tempfile.TemporaryDirectory(prefix='lulu-installer-build-') as tmp:
        source=Path(tmp)/'installer.c';source.write_text(LAUNCHER_SOURCE)
        subprocess.run(['/usr/bin/xcrun','clang','-mmacosx-version-min=15.0','-Wall','-Wextra','-Werror',str(source),'-o',str(macos/'InstallLulu')],check=True)
    subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(app)],check=True)
    for flags in (['--strict'],['--deep','--strict']):
        subprocess.run(['/usr/bin/codesign','--verify',*flags,str(app)],check=True)


def create_delivery(app,output):
    from src.core.agent_entry.standalone_source import DESCRIPTOR
    if (Path(app)/DESCRIPTOR).exists():
        from create_drag_delivery import create_delivery as create_drag
        return create_drag(app, output)
    app=Path(app).resolve();output=Path(output).resolve();manifest=app.parent/MANIFEST_NAME
    verify_component_manifest(app,manifest);verify_final_signature(app)
    if output.exists():raise RuntimeError('DELIVERY_OUTPUT_MUST_BE_NEW')
    work=output.parent.parent/('.'+output.stem+'-delivery-evidence');work.mkdir()
    payload=work/'payload';payload.mkdir()
    shutil.copytree(app,payload/'Lulu.app',symlinks=True);shutil.copy2(manifest,payload/MANIFEST_NAME)
    create_installer(payload/'Install Lulu.app')
    verify_component_manifest(payload/'Lulu.app',payload/MANIFEST_NAME)
    subprocess.run(['/usr/bin/hdiutil','create','-srcfolder',str(payload),'-volname','安装 Lulu','-fs','HFS+',
        '-format','UDZO',str(output)],check=True)
    digest,_metadata=_read(output,digest_only=True)
    output.with_name(output.name+'.sha256').write_text(digest+'  '+output.name+'\n')
    proof=verify_delivery_dmg(output,expected_app=app,expected_sha256=digest,
        stage_root=work/'verified-stage')
    (work/'receipt.json').write_text(json.dumps(proof.receipt,indent=2)+'\n')
    (output.parent/'安装说明.txt').write_text('请保留本文件夹内的安装包与配套校验文件。\n打开安装包，双击“安装 Lulu”。完成后会打开 Lulu。\nLulu Agent Skill 可在 Lulu 设置中按需安装。\n')
    archive=output.parent.with_name(output.parent.name+'.zip')
    if archive.exists():raise RuntimeError('DELIVERY_OUTPUT_MUST_BE_NEW')
    subprocess.run(['/usr/bin/ditto','-c','-k','--keepParent',str(output.parent),str(archive)],check=True)
    return proof.receipt

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--app',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(create_delivery(args.app,args.output)))
