#!/usr/bin/env python3
"""Install/remove only this optional Skill. Python stdlib; no network or App writes."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

NAME='lulu-agent'
RECEIPT='.lulu-agent-public-install.json'
SCHEMA='lulu.public-skill-install/v1'


def safe(path):
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('符号链接路径不支持，请选择实际技能目录。')
    return path


def manifest(folder):
    safe(folder)
    result={}
    for p in sorted(folder.rglob('*')):
        safe(p)
        if p.is_file():
            rel=p.relative_to(folder).as_posix()
            if rel!=RECEIPT:result[rel]=hashlib.sha256(p.read_bytes()).hexdigest()
        elif not p.is_dir():raise ValueError('技能包含非常规文件。')
    if 'SKILL.md' not in result:raise ValueError('缺少 SKILL.md。')
    return result


def manage(action,skills_dir,source=None):
    directory=Path(os.path.abspath(Path(skills_dir).expanduser()))
    safe(directory)
    target=directory/NAME
    if target==Path.home()/'.agents/skills/lulu-agent':
        raise ValueError('这是 Lulu 旧版受管位置，请使用当前 Agent 的其他技能目录。')
    source=Path(source) if source else Path(__file__).resolve().parent/NAME
    if action=='uninstall' and not directory.exists():return '未安装，无需卸载。'
    directory.mkdir(parents=True,exist_ok=True)
    # Lock the existing directory itself: no leftover lock file in skills catalog.
    fd=os.open(directory,os.O_RDONLY)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        safe(target)
        if action=='install':
            if target.exists():raise ValueError('已有同名技能，已保留。请先核对和卸载旧副本。')
            expected=manifest(source)
            with tempfile.TemporaryDirectory(prefix='.lulu-install-',dir=directory) as tmp:
                stage=Path(tmp)/NAME
                shutil.copytree(source,stage)
                if manifest(stage)!=expected:raise ValueError('复制校验失败，未安装。')
                (stage/RECEIPT).write_text(json.dumps(dict(schema=SCHEMA,files=expected),sort_keys=True)+'\n')
                stage.rename(target)
            return '已安装：'+str(target)
        if action!='uninstall':raise ValueError('未知操作。')
        if not target.exists():return '未安装，无需卸载。'
        receipt=safe(target/RECEIPT)
        if not receipt.is_file():raise ValueError('此副本不由本安装器管理，已保留。请使用原安装方式卸载。')
        saved=json.loads(receipt.read_text())
        if saved.get('schema')!=SCHEMA or saved.get('files')!=manifest(target):
            raise ValueError('技能内容已有修改，已保留。请先备份并检查修改。')
        shutil.rmtree(target)
        return '已卸载技能文件，Lulu 与个人资料保持原样。'
    finally:os.close(fd)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','uninstall']);p.add_argument('--skills-dir',required=True)
    a=p.parse_args()
    try:print(manage(a.action,a.skills_dir))
    except (OSError,ValueError) as exc:p.exit(1,str(exc)+'\n')

if __name__=='__main__':main()
