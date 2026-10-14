"""Explicit Chat Completions and Messages adapters, without request retries.

Keep the provider's assistant envelope for a subsequent tool round. Hidden
reasoning is never displayed or used as the document replacement.
"""
from __future__ import annotations

import json
import httpx
from .providers import key_for, validate_url

MAX_RESPONSE = 8 * 1024 * 1024


class ModelInterrupted(RuntimeError):
    pass


def _events(lines, cancelled):
    """SSE data may span several lines; comments and keepalives carry no data."""
    parts, size = [], 0
    for line in lines:
        if cancelled():
            raise ModelInterrupted('任务已取消，已完成的修改与返回内容保留')
        size += len(line.encode())
        if size > MAX_RESPONSE:
            raise RuntimeError('模型结果超过单次接收范围，已有内容保留')
        if not line:
            if parts:
                yield '\n'.join(parts)
                parts = []
        elif line.startswith('data:'):
            parts.append(line[5:].lstrip(' '))
    if parts:
        yield '\n'.join(parts)


def _request(config, system, messages, tools):
    base = validate_url(config['base'])
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    token = key_for(config)
    if config['protocol'] == 'anthropic':
        url = base + ('/messages' if base.endswith('/v1') else '/v1/messages')
        headers.update({'x-api-key': token, 'anthropic-version': '2023-06-01'})
        payload = dict(model=config['model'], system=system, messages=messages,
                       max_tokens=config['max_output'], stream=True)
        if tools:
            payload['tools'] = [dict(name=t['name'], description=t['description'], input_schema=t['parameters']) for t in tools]
    elif config['protocol'] == 'openai':
        url = base + '/chat/completions'
        headers['Authorization'] = 'Bearer ' + token
        payload = dict(model=config['model'], messages=[{'role': 'system', 'content': system}, *messages], stream=True)
        output_field = 'max_completion_tokens' if config.get('provider') == 'mimo' else 'max_tokens'
        payload[output_field] = config['max_output']
        if tools:
            payload['tools'] = [{'type': 'function', 'function': t} for t in tools]
    else:
        raise ValueError('当前连接的接口协议无效')
    return url, headers, payload


def stream(config, system, messages, *, tools=(), on_text=lambda text: None,
           on_phase=lambda phase: None, cancelled=lambda: False):
    url, headers, payload = _request(config, system, messages, tools)
    anthropic = config['protocol'] == 'anthropic'
    text, calls, blocks, usage = '', {}, {}, {}
    reasoning, stop_reason, complete = '', None, False
    try:
        on_phase('connecting')
        with httpx.Client(timeout=httpx.Timeout(90, connect=20), follow_redirects=False, trust_env=False) as client:
            if cancelled():
                raise ModelInterrupted('任务已取消')
            with client.stream('POST', url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    explanations = {401: '密钥无效或已过期', 402: '余额或套餐额度不足', 403: '当前密钥没有访问权限',
                                    404: '模型或接口地址不可用', 429: '请求频率或额度受限'}
                    reason = explanations.get(response.status_code, '服务暂时不可用')
                    raise RuntimeError(f'{reason}（HTTP {response.status_code}）；没有切换供应商或重复发送')
                on_phase('waiting_first_token')
                for raw in _events(response.iter_lines(), cancelled):
                    if raw == '[DONE]':
                        complete = stop_reason in {'stop', 'tool_calls'}
                        break
                    item = json.loads(raw)
                    if not isinstance(item, dict) or item.get('error'):
                        raise RuntimeError('模型返回错误，已有部分内容保留')
                    if anthropic:
                        kind = item.get('type')
                        if kind == 'error':
                            raise RuntimeError('模型返回错误，已有部分内容保留')
                        if kind == 'message_start':
                            usage.update(item.get('message', {}).get('usage', {}))
                        elif kind == 'content_block_start':
                            block = dict(item['content_block'])
                            index = item['index']
                            if index in blocks:
                                raise ValueError('duplicate block')
                            blocks[index] = block
                            if block['type'] == 'text':
                                text += block.get('text', '')
                                if block.get('text'):
                                    on_phase('streaming')
                                    on_text(text)
                            elif block['type'] == 'tool_use':
                                calls[index] = dict(name=block['name'], id=block['id'], arguments='', initial=block.get('input', {}))
                        elif kind == 'content_block_delta':
                            delta, index = item['delta'], item['index']
                            block = blocks[index]
                            if delta['type'] == 'text_delta':
                                fragment = delta.get('text', '')
                                block['text'] = block.get('text', '') + fragment
                                text += fragment
                                if fragment:
                                    on_phase('streaming')
                                    on_text(text)
                            elif delta['type'] == 'input_json_delta':
                                calls[index]['arguments'] += delta.get('partial_json', '')
                            elif delta['type'] == 'thinking_delta':
                                block['thinking'] = block.get('thinking', '') + delta.get('thinking', '')
                            elif delta['type'] == 'signature_delta':
                                block['signature'] = block.get('signature', '') + delta.get('signature', '')
                        elif kind == 'message_delta':
                            usage.update(item.get('usage', {}))
                            stop_reason = item.get('delta', {}).get('stop_reason') or stop_reason
                        elif kind == 'message_stop':
                            complete = stop_reason in {'end_turn', 'tool_use', 'stop_sequence'}
                    else:
                        usage.update(item.get('usage') or {})
                        choices = item.get('choices', [])
                        if len(choices) > 1:
                            raise ValueError('multiple alternatives')
                        for choice in choices:
                            stop_reason = choice.get('finish_reason') or stop_reason
                            delta = choice.get('delta') or {}
                            if delta.get('content'):
                                text += delta['content']
                                on_phase('streaming')
                                on_text(text)
                            reasoning += delta.get('reasoning_content') or ''
                            for part in delta.get('tool_calls', []):
                                call = calls.setdefault(part['index'], dict(id='', name='', arguments=''))
                                call['id'] += part.get('id') or ''
                                call['name'] += part.get('function', {}).get('name') or ''
                                call['arguments'] += part.get('function', {}).get('arguments') or ''
                    if stop_reason in {'length', 'max_tokens'}:
                        raise RuntimeError('回答达到输出上限，保留部分内容；未应用不完整建议')
                    if stop_reason in {'content_filter', 'refusal'}:
                        raise RuntimeError('模型没有完成这次请求，已有回复保留；未应用修改')
    except httpx.TimeoutException:
        raise RuntimeError('模型请求超时，服务是否仍在处理无法确认；未自动重发') from None
    except httpx.HTTPError:
        raise RuntimeError('模型连接中断，服务是否已处理无法确认；未自动重发') from None
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('模型返回的数据格式不完整，已有内容保留；未应用修改') from None
    if cancelled():
        raise ModelInterrupted('任务已停止，结果不会应用正文')
    if not complete:
        raise RuntimeError('模型连接提前结束，部分内容保留，未自动重发')
    parsed, ids = [], set()
    for index, call in sorted(calls.items()):
        try:
            arguments = json.loads(call['arguments']) if call['arguments'] else call.get('initial', {})
        except (ValueError, TypeError):
            raise RuntimeError('模型返回的修改参数不完整，未应用修改') from None
        if not isinstance(arguments, dict) or not call.get('id') or not call.get('name') or call['id'] in ids:
            raise RuntimeError('模型返回的工具信息无效，未执行')
        ids.add(call['id'])
        parsed.append(dict(id=call['id'], name=call['name'], arguments=arguments))
        if anthropic:
            blocks[index]['input'] = arguments
    if not text.strip() and not parsed:
        raise RuntimeError('模型没有返回文字或修改建议，未更改正文')
    if parsed and stop_reason not in {'tool_calls', 'tool_use'}:
        raise RuntimeError('模型没有完成工具调用，建议未应用')
    if anthropic:
        assistant = {'role': 'assistant', 'content': [v for _, v in sorted(blocks.items())]}
    else:
        assistant = {'role': 'assistant', 'content': text or None}
        if reasoning:
            assistant['reasoning_content'] = reasoning
        if parsed:
            assistant['tool_calls'] = [dict(id=c['id'], type='function', function=dict(name=c['name'],
                                       arguments=json.dumps(c['arguments'], ensure_ascii=False))) for c in parsed]
    return dict(text=text, calls=parsed, usage=usage, assistant=assistant)
