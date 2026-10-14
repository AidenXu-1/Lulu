import unittest
from src.core.agent_workspace.references import bind_instructions
from src.core.agent_workspace.documents import digest


class ReferenceInstructionTests(unittest.TestCase):
    def setUp(self):
        self.doc = {'text':'第一段。\n第二段。'}
        self.refs = [dict(id='one',label_number=1,start=0,end=12,markdown='第一段。',before_hash=digest('第一段。')),
                     dict(id='two',label_number=2,start=13,end=25,markdown='第二段。',before_hash=digest('第二段。'))]

    def test_empty_labels_do_not_send_and_unrequested_fragment_is_excluded(self):
        with self.assertRaisesRegex(ValueError,'具体要求'):
            bind_instructions('片段一：\n片段二：',self.refs,self.doc)
        _, refs=bind_instructions('片段一：精简\n片段二：',self.refs,self.doc)
        self.assertEqual([r['id'] for r in refs],['one'])
        self.assertEqual(refs[0]['instruction'],'精简')

    def test_common_instruction_applies_to_both_and_wrong_label_is_rejected(self):
        _,refs=bind_instructions('保留事实\n片段一：精简\n片段二：',self.refs,self.doc)
        self.assertEqual([r['instruction'] for r in refs],['保留事实\n精简','保留事实'])
        with self.assertRaisesRegex(ValueError,'片段三'):
            bind_instructions('片段三：精简',self.refs,self.doc)

    def test_forged_or_overlapping_reference_is_rejected(self):
        self.refs[0]['markdown']='其他内容'
        with self.assertRaisesRegex(ValueError,'不一致'):
            bind_instructions('精简',self.refs,self.doc)

    def test_removed_reference_instructions_never_default_to_whole_document(self):
        with self.assertRaisesRegex(ValueError, '引用已移除'):
            bind_instructions('片段一：精简', [], self.doc)
        self.assertEqual(bind_instructions('把整篇文稿精简', [], self.doc), ('把整篇文稿精简', []))

    def test_numeric_labels_bind_to_same_reference_and_alias_duplicates_fail(self):
        _, refs=bind_instructions('片段1：精简\n片段2：保留数字',self.refs,self.doc)
        self.assertEqual([r['instruction'] for r in refs],['精简','保留数字'])
        with self.assertRaisesRegex(ValueError,'唯一引用'):
            bind_instructions('片段一：精简\n片段1：保留',self.refs,self.doc)

class FormattedSourceRangeTests(unittest.TestCase):
    def test_long_transcript_reference_is_prompt_and_exact(self):
        import time
        from unittest.mock import patch
        from src.core.agent_workspace.service import WorkspaceService
        original=''.join(f'**[{i//60:02d}:{i%60:02d}]** 重复的中文逐字稿。保持事实与说话顺序。\n\n' for i in range(1200))
        canonical=original.replace('**[','**\\[')
        a=canonical.index('**\\[15:00]');b=canonical.index('**\\[15:03]')
        start,end=len(canonical[:a].encode()),len(canonical[:b].encode())-2
        expected=original[original.index('**[15:00]'):original.index('**[15:03]')].rstrip('\n')
        doc=dict(id='long-transcript',text=original,revision=1,content_hash=digest(original))
        began=time.monotonic()
        with patch('src.core.agent_workspace.documents.snapshot',return_value=doc):
            ref=WorkspaceService().reference(doc['id'],canonical,start,end,1,doc['content_hash'])
        self.assertEqual(ref['markdown'],expected)
        self.assertEqual(original.encode()[ref['start']:ref['end']].decode(),expected)
        self.assertEqual(ref['before_hash'],digest(expected))
        # Generous interaction budget; the former whole-document diff took >12 s.
        self.assertLess(time.monotonic()-began,2.0)

    def test_code_fence_normalization_keeps_selected_code(self):
        from src.core.agent_workspace.references import formatted_source_range
        original='~~~python\nprint("中文")\n~~~\n'
        canonical=original.replace('~~~','```')
        a=canonical.index('print');b=canonical.index('\n```')
        start,end=len(canonical[:a].encode()),len(canonical[:b].encode())
        x,y=formatted_source_range(canonical,original,start,end)
        self.assertEqual(original.encode()[x:y].decode(),'print("中文")')

    def test_cross_block_escape_mapping_uses_same_occurrence(self):
        from src.core.agent_workspace.references import formatted_source_range
        original=''.join(f'**[{i:02d}:00]** 重复内容。\n\n' for i in range(50))
        canonical=original.replace('**[','**\\[')
        a=canonical.index('\\[20:00]');b=canonical.index('重复内容。',canonical.index('[22:00]'))+len('重复内容。')
        start,end=len(canonical[:a].encode()),len(canonical[:b].encode())
        x,y=formatted_source_range(canonical,original,start,end)
        text=original.encode()[x:y].decode()
        self.assertTrue(text.startswith('[20:00]'))
        self.assertTrue(text.endswith('重复内容。'))
        self.assertEqual(text.count('重复内容。'),3)

    def test_semantic_or_destination_change_never_maps(self):
        from src.core.agent_workspace.references import formatted_source_range
        original='**[00:00]** 中文原文。\n\n[来源](https://example.test/original)\n'
        for changed in [original.replace('原文','改动'),original.replace('/original','/different')]:
            self.assertIsNone(formatted_source_range(changed,original,0,10))

    def test_cover_autolink_normalization_keeps_exact_text_and_target(self):
        from src.core.agent_workspace.references import formatted_source_range
        original = '![封面](file:///fixture/a.jpg)\n\n> 来源：https://example.test/item?a=1&b=2\n\n**[00:16]** 原文。\n'
        canonical = original.replace('**[', '**\\[').replace('https://example.test/item?a=1&b=2', '<https://example.test/item?a=1&b=2>')
        start = len(canonical[:canonical.index('**\\[')].encode())
        end = len(canonical.rstrip().encode())
        x, y = formatted_source_range(canonical, original, start, end)
        self.assertEqual(original.encode()[x:y].decode(), '**[00:16]** 原文。')
        for changed in [canonical.replace('a.jpg','b.jpg'), canonical.replace('a=1','a=9'), canonical.replace('原文','改动')]:
            self.assertIsNone(formatted_source_range(changed, original, start, end))
