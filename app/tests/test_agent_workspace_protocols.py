"""Offline wire tests. No real credentials or external requests."""
import json
import unittest
from unittest.mock import patch
import httpx
from src.core.agent_workspace import model_adapter as adapter


def sse(items):
    return ''.join('data: '+(v if isinstance(v,str) else json.dumps(v))+'\n\n' for v in items)


class ProtocolTests(unittest.TestCase):
    def run_stream(self, items, protocol='openai', status=200, **options):
        seen=[]
        client=httpx.Client
        def request(req):
            seen.append(json.loads(req.content))
            return httpx.Response(status,text=sse(items),headers={'content-type':'text/event-stream'})
        with patch.object(adapter,'key_for',return_value='fixture-key'), patch.object(adapter.httpx,'Client',side_effect=lambda **kw:client(transport=httpx.MockTransport(request),**kw)):
            result=adapter.stream(dict(protocol=protocol,provider='mimo',base='https://fixture.invalid/v1',model='fixture',max_output=2048),'系统',[{'role':'user','content':'改稿'}],**options)
        return result,seen

    def test_openai_tool_fragments_and_reasoning_envelope(self):
        events=[{'choices':[{'delta':{'reasoning_content':'private','tool_calls':[{'index':0,'id':'call1','function':{'name':'propose_edits','arguments':'{"x":'}}]}}]},
                {'choices':[{'delta':{'content':'建议','tool_calls':[{'index':0,'function':{'arguments':'1}'}}]},'finish_reason':'tool_calls'}]},
                {'choices':[],'usage':{'prompt_tokens':42}},'[DONE]']
        result,seen=self.run_stream(events)
        self.assertEqual(result['calls'][0]['arguments'],{'x':1})
        self.assertEqual(result['text'],'建议')
        self.assertEqual(result['assistant']['reasoning_content'],'private')
        self.assertEqual(seen[0]['max_completion_tokens'],2048)
        self.assertNotIn('max_tokens',seen[0])

    def test_anthropic_initial_input_and_signed_thinking_are_preserved(self):
        result,_=self.run_stream([
            {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
            {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'private'}},
            {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed'}},
            {'type':'content_block_start','index':1,'content_block':{'type':'text','text':'建议'}},
            {'type':'content_block_start','index':2,'content_block':{'type':'tool_use','id':'call1','name':'propose_edits','input':{'x':1}}},
            {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':22}},
            {'type':'message_stop'}],protocol='anthropic')
        self.assertEqual(result['text'],'建议')
        self.assertEqual(result['calls'][0]['arguments'],{'x':1})
        self.assertEqual(result['assistant']['content'][0],dict(type='thinking',thinking='private',signature='signed'))

    def test_visible_phases_follow_connection_first_text_and_stream_events(self):
        phases=[]
        result,_=self.run_stream([
            {'choices':[{'delta':{'content':'正在回复'},'finish_reason':None}]},
            {'choices':[{'delta':{},'finish_reason':'stop'}]},'[DONE]'],on_phase=phases.append)
        self.assertEqual(result['text'],'正在回复')
        self.assertEqual(phases,['connecting','waiting_first_token','streaming'])

    def test_truncated_or_filtered_responses_never_complete(self):
        for events in ([{'choices':[{'delta':{'content':'partial'}}]},'[DONE]'],
                       [{'choices':[{'delta':{'content':'partial'},'finish_reason':'length'}]},'[DONE]'],
                       [{'choices':[{'delta':{},'finish_reason':'content_filter'}]},'[DONE]'],[{'type':'message_stop'}]):
            with self.subTest(events=events),self.assertRaises(RuntimeError):self.run_stream(events)
        with self.assertRaisesRegex(RuntimeError,'接口地址'):self.run_stream([],status=404)

    def test_multiline_sse_and_cancellation(self):
        events=list(adapter._events([': heartbeat','data: {','data: "a":1}',''],lambda:False))
        self.assertEqual(json.loads(events[0]),{'a':1})
        with self.assertRaises(adapter.ModelInterrupted):list(adapter._events(['data: {}',''],lambda:True))
