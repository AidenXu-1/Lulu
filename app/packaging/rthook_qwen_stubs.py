"""PyInstaller 运行时钩子:给 nagisa.train 的 Python2 风格裸导入兜底。

qwen-asr 依赖 nagisa(分词,Qwen3 强制对齐/时间戳会用到)。`import nagisa` 会经
__init__ 的 `from nagisa.train import fit` 连带加载 train.py,而 train.py 用
`import prepro / model / mecab_system_eval` 裸导入同目录文件——冻结后它们被打成
nagisa.prepro 等子模块,裸名导入失败。

用空 stub 让这几个裸名导入通过即可:train.fit() 在推理期从不调用,空实现无副作用;
真正干活的分词推理走 tagger.py 里的 `import nagisa.model as model`(带包名的限定导入),
不受 sys.modules['model'] 这个空 stub 影响——所以即使开了时间戳/强制对齐也安全。
funasr 加载 SenseVoice 远程码时对 `model` 的导入失败本就有兜底,空 stub 同样会被接住。
"""

import sys
import types

for _name in ("prepro", "model", "mecab_system_eval"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)
