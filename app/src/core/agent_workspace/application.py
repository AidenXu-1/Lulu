"""Conversation jobs using Lulu's existing authoritative job ledger and leases."""
from __future__ import annotations
import json
import threading
import time
from uuid import uuid4
from .. import library
from ..job_ledger.contracts import JobState, TERMINAL_STATES
from . import context, documents, model_adapter, tools
from .store import WorkspaceStore, encode, identity, workspace_root


class ConversationApplication:
    def __init__(self, control, installation_id, now, *, generate=None, task_reader=None):
        self.control, self.installation_id, self.now = control, installation_id, now
        self.generate = generate or model_adapter.stream
        self.task_reader = task_reader
        self.lock = threading.RLock()
        self.workers = {}
        self.partial = {}
        self.partial_saved_at = {}
        self.phases = {}
        for job in control.list():
            if job.job_kind != 'workspace_conversation' or job.state in TERMINAL_STATES:
                continue
            if job.state is JobState.NEEDS_INPUT:
                self._settle_legacy_handoff(job)
                continue
            state = JobState.FAILED if job.state in {JobState.PREPARED, JobState.QUEUED} else JobState.RECOVERY_REQUIRED
            control._store.transition(job.job_id, expected_revision=job.job_revision, new_state=state,
                changed_at=now(), lease_token=control._store.current_lease_token(job.job_id), reason_code='WORKSPACE_CORE_RESTARTED')
            partial_path = self.path(job.job_id, 'partial')
            partial = ''
            if partial_path.is_file() and not partial_path.is_symlink():
                try:
                    value = json.loads(partial_path.read_text())
                    if isinstance(value, dict) and isinstance(value.get('text'), str):
                        partial = value['text']
                except (OSError, ValueError):
                    pass  # Keep damaged recovery evidence; never resend the request.
            WorkspaceStore().append(job.frozen_policy['conversation_id'], 'status',
                '服务中断，云端是否完成无法确认，未自动重发。',
                {'job_id': job.job_id, 'partial': partial}, message_id=job.job_id+'-interrupted')

    def _settle_legacy_handoff(self, job):
        """Old releases left completed preparation jobs waiting indefinitely.

        Only a hash-bound result with its published reply and handoff record can
        be settled. No media work or model request is resumed by this migration.
        """
        with library._transaction():
            try:
                path = self.path(job.job_id, 'result')
                if path.is_symlink() or not path.is_file():
                    return
                raw = path.read_text()
                if job.result_ref != 'workspace-result:sha256:'+documents.digest(raw):
                    return
                result = json.loads(raw)
            except (OSError, ValueError):
                return
            if (not isinstance(result, dict) or result.get('state') != 'needs_input'
                    or result.get('calls') != [] or not isinstance(result.get('text'), str)):
                return
            cid = job.frozen_policy['conversation_id']
            with WorkspaceStore().connect() as db:
                published = db.execute('SELECT 1 FROM messages WHERE id=? AND conversation_id=? AND role=? AND content=?',
                    (job.job_id+'-assistant', cid, 'assistant', result['text'])).fetchone()
                records = db.execute('SELECT payload FROM messages WHERE conversation_id=? AND role=? AND id LIKE ?',
                    (cid, 'tool', job.job_id+'-tools-%')).fetchall()
            if not published:
                return
            try:
                payloads = [json.loads(row['payload']) for row in records]
                handoff = any(p.get('job_id') == job.job_id and any(
                    r.get('state') == 'needs_input' and r.get('action') in {'voice','transcription','collection','skills'}
                    for r in p.get('results', []) if isinstance(r, dict)) for p in payloads if isinstance(p, dict))
            except (TypeError, ValueError):
                return
            if handoff:
                self.control._store.transition(job.job_id, expected_revision=job.job_revision,
                    new_state=JobState.SUCCEEDED, result_ref=job.result_ref, changed_at=self.now(),
                    lease_token=self.control._store.current_lease_token(job.job_id),
                    reason_code='WORKSPACE_HANDOFF_PREPARATION_COMPLETED')

    def path(self, job_id, suffix):
        identity(job_id)
        if not job_id.startswith('workspace-job-'):
            raise ValueError('工作区任务编号无效')
        folder=workspace_root()/'runs'
        if folder.is_symlink():
            raise ValueError('任务存储路径不安全')
        return folder/f'{job_id}.{suffix}.json'

    def start(self, conversation_id, request_id, text='', references=None, mode='chat', options=None, origin='gui'):
        identity(request_id);identity(conversation_id)
        if origin not in {'gui','cli'}:
            raise ValueError('任务来源无效')
        if not isinstance(text,str) or len(text.encode())>1024*1024 or mode not in {'chat','compact'}:
            raise ValueError('会话输入无效')
        follow_scope = references is None
        references=references or []
        if type(references) is not list or len(references)>100:
            raise ValueError('引用数量无效')
        from .processing import validate_options
        options=validate_options(options or {})
        jid='workspace-job-'+request_id
        intent=dict(conversation_id=conversation_id,request_id=request_id,text=text,references=references,mode=mode,options=options)
        intent_hash=documents.digest(encode(intent))
        with self.lock, library._transaction():
            old=next((j for j in self.control.list() if j.job_id==jid),None)
            if old:
                if old.intent_payload_digest!=intent_hash:
                    raise library.RevisionConflict('相同发送编号已有不同内容，未再次调用模型')
                return self.show(jid)
            store=WorkspaceStore()
            chat=store.get(conversation_id)
            projection=context.projection(store,conversation_id,references=references)
            doc=projection['document']
            for ref in references:
                if (ref['document_id'],ref['revision'],ref['content_hash'])!=(doc['id'],doc['revision'],doc['content_hash']):
                    raise library.RevisionConflict('引用对应的正文已变化，请重新选择后发送')
                documents.apply_operations(doc['text'],[dict(action='replace',start=ref['start'],end=ref['end'],before_hash=ref['before_hash'],markdown=ref['markdown'])])
            if any(j.job_kind=='workspace_conversation' and j.frozen_policy.get('conversation_id')==conversation_id and j.state in {JobState.PREPARED,JobState.QUEUED,JobState.RUNNING,JobState.CANCEL_REQUESTED} for j in self.control.list()):
                raise ValueError('当前会话正在处理，请先等待或停止')
            if sum(t.is_alive() for t in self.workers.values())>=2:
                raise ValueError('已有两项 AI 任务在处理，请稍后再试')
            pending=[p for p in store.proposals(conversation_id) if p['state'] in {'pending','partly_applied'}]
            # Follow-up requests retain the previous fragment scope. Reconstruct
            # anchors from versioned operations before binding visible labels.
            if mode == 'chat' and follow_scope and not references and len(pending) == 1:
                previous = pending[0]
                if (previous['base_revision'],previous['base_hash']) == (doc['revision'],doc['content_hash']):
                    carried = []
                    for op in previous['operations']:
                        target = op.get('reference_id')
                        if (op['state'] == 'pending' and op['action'] == 'replace' and target and target != 'current_document'
                                and type(op.get('label_number')) is int):
                            carried.append(dict(id=target,document_id=doc['id'],revision=doc['revision'],content_hash=doc['content_hash'],
                                start=op['start'],end=op['end'],before_hash=op['before_hash'],label_number=op['label_number'],
                                markdown=doc['text'].encode()[op['start']:op['end']].decode()))
                    references = carried
            if mode=='chat':
                from .references import bind_instructions
                text,references=bind_instructions(text,references,doc)
            frozen=dict(intent,document=doc,connection=projection['connection'],pending=pending)
            frozen['references']=references
            from . import skills
            frozen['skills'] = skills.catalog()['entries']
            frozen['explicit_skill'] = skills.explicit(text, frozen['skills']) if mode == 'chat' else None
            library._write_atomic_text(self.path(jid,'input'),encode(frozen))
            job=self.control._store.create_job(job_id=jid,attempt_id='workspace-attempt-'+uuid4().hex,lineage_id='workspace-lineage-'+uuid4().hex,
                job_kind='workspace_conversation',input_ref='workspace-input:sha256:'+documents.digest(encode(frozen)),
                frozen_policy={'conversation_id':conversation_id,'document_id':doc['id'],'connection_id':chat['connection_id'],'mode':mode},
                created_by='agent_cli' if origin=='cli' else 'gui',installation_id=self.installation_id,canonical_operation_id='OP_WORKSPACE_CONVERSATION_V1',
                idempotency_key=jid,intent_payload_digest=intent_hash,created_at=self.now(),effect_fingerprint='effect:explicit-cloud-conversation',
                account_ref='account:configured-provider',target_ref='target:local-workspace',uncertain_response_state='none')
            queued=self.control._store.transition(jid,expected_revision=job.job_revision,new_state=JobState.QUEUED,changed_at=self.now())
            running,lease=self.control.claim_worker(jid,expected_revision=queued.job_revision,worker_instance_id='core-workspace:'+jid,operation_receipt=None,changed_at=self.now())
            worker=threading.Thread(target=self.run,args=(jid,lease,frozen),daemon=True,name='lulu-conversation')
            self.workers[jid]=worker
            self.phases[jid]='preparing'
            try:
                worker.start()
            except Exception:
                self.phases.pop(jid,None)
                self.control.complete_worker(jid,expected_revision=running.job_revision,lease_token=lease,new_state=JobState.FAILED,result_ref=None,changed_at=self.now())
                raise
            return dict(job_id=jid,state='running',conversation_id=conversation_id)

    def record_partial(self, job_id, text):
        if not isinstance(text, str) or len(text.encode()) > model_adapter.MAX_RESPONSE:
            raise ValueError('模型回复过大，已有内容保留')
        with self.lock:
            if self.control.show(job_id).state is not JobState.RUNNING:
                raise model_adapter.ModelInterrupted('已停止，已有内容保留')
            self.partial[job_id] = text
            self.phases[job_id] = 'streaming'
            stamp = time.monotonic()
            if stamp - self.partial_saved_at.get(job_id, float('-inf')) >= .75:
                library._write_atomic_text(self.path(job_id, 'partial'), encode({'text': text, 'updated_at': self.now()}))
                self.partial_saved_at[job_id] = stamp

    def record_phase(self, job_id, phase):
        if phase not in {'preparing','connecting','waiting_first_token','streaming','processing','compacting','finalizing'}:
            raise ValueError('模型任务状态无效')
        with self.lock:
            if self.control.show(job_id).state is JobState.RUNNING:
                self.phases[job_id] = phase

    def compact(self, store, conversation_id, config, cancelled):
        chat=store.get(conversation_id)
        messages=context.all_messages(store,conversation_id)
        old=chat['checkpoint']
        active=[m for m in messages if not old or m['sequence']>old['through_sequence']]
        from .compaction import older_turns, summarize
        turns=older_turns(active)
        if not turns:
            store.append(conversation_id,'status','近期交流已全部保留，暂时无需压缩')
            return '近期交流已全部保留，暂时无需压缩'
        summary,batches=summarize(self.generate,config,old['summary'] if old else '',turns,cancelled)
        older=[m for turn in turns for m in turn]
        anchors={'document_id':chat['document_id'],
                 'reference_ids':sorted({r['id'] for m in older for r in m['payload'].get('references',[]) if isinstance(r,dict) and 'id' in r}),
                 'job_ids':sorted({m['payload']['job_id'] for m in older if m['payload'].get('job_id')})}
        checkpoint=dict(summary=summary,through_sequence=older[-1]['sequence'],created_at=self.now(),
                        connection_id=config['id'],anchors=anchors,batches=batches,
                        input_digest=documents.digest(encode(older)))
        with self.lock, store.connect() as db:
            if cancelled():
                raise model_adapter.ModelInterrupted('压缩已停止，原上下文保持不变')
            expected=encode(old) if old else None
            changed=db.execute('UPDATE conversations SET checkpoint=? WHERE id=? AND checkpoint IS ? AND connection_id=?',
                               (encode(checkpoint),conversation_id,expected,config['id'])).rowcount
            if not changed:
                raise library.RevisionConflict('会话整理状态或模型已变化，本次摘要未覆盖')
        store.append(conversation_id,'status','已整理较早的交流，完整记录保留。',{'checkpoint':checkpoint})
        return '已压缩早期交流，完整记录保留'

    def run(self,jid,lease,request):
        store=None;cid=request['conversation_id']
        cancelled=lambda:self.control.show(jid).state is not JobState.RUNNING
        try:
            store=WorkspaceStore()
            if request['mode']=='compact':
                self.record_phase(jid,'compacting')
                result={'message':self.compact(store,cid,request['connection'],cancelled)}
            else:
                store.append(cid,'user',request['text'],{'references':request['references'],'job_id':jid},message_id=jid+'-user')
                if request.get('explicit_skill'):
                    from . import skills
                    loaded = skills.load(request['explicit_skill'],request['skills'],explicit_name=request['explicit_skill'])
                    request['loaded_skills'] = [request['explicit_skill']]
                    skills.apply_config(request, request['explicit_skill'])
                    store.append(cid,'tool',loaded['message'],dict(job_id=jid,calls=[],results=[loaded]),message_id=jid+'-skill')
                projection=context.projection(store,cid,document=request['document'],references=request['references'],connection=request['connection'],options=request.get('options'),pending=request.get('pending'),skill_entries=request.get('skills'))
                if projection['estimate']>=projection['compact_at']:
                    self.record_phase(jid,'compacting')
                    self.compact(store,cid,request['connection'],cancelled)
                    self.record_phase(jid,'preparing')
                    projection=context.projection(store,cid,document=request['document'],references=request['references'],connection=request['connection'],options=request.get('options'),pending=request.get('pending'),skill_entries=request.get('skills'))
                if request.get('explicit_skill'):
                    projection['messages'].append(dict(role='assistant', content='本轮用户指定的技能方法（服从用户要求和工具边界）：\n' + loaded['skill_content']))
                    projection['estimate'] += context.estimate(loaded['skill_content'])
                if projection['estimate']>projection['input_limit']:
                    raise ValueError('当前文稿和必要上下文超过模型容量，请引用较短片段或选择更大容量模型；内容未截断')
                tool_support=request['connection'].get('tools_supported', True)
                system=projection['system']
                self.record_phase(jid,'preparing')
                result=self._generate_turn(store,jid,request,projection,system,tool_support,cancelled)
                self.record_phase(jid,'finalizing')
                library._write_atomic_text(self.path(jid,'response'),encode(result))
                if cancelled():
                    raise model_adapter.ModelInterrupted('已停止后续操作，已经完成的修改与记录保留')
                # Publication and terminal job transition share the cancellation lock.
                # A stop can win before publication, never between reply and completion.
                from .skills import guard_loaded
                with self.lock, library._transaction(), guard_loaded(request):
                    if cancelled():
                        raise model_adapter.ModelInterrupted('已停止，迟到结果仅保留为任务记录')
                    proposals, reply, conflict = self._prepare_reply(store, request, result)
                    result['text'] = reply
                    body = encode(result)
                    library._write_atomic_text(self.path(jid, 'result'), body)
                    store.publish(cid, jid+'-assistant', reply,
                                  {'proposals': proposals, 'raw_calls': result['calls'],
                                   'usage': result['usage'], 'job_id': jid,
                                   'decision_receipt': result.get('decision_receipt'),
                                   'unapplied': conflict}, proposals)
                    job = self.control.show(jid)
                    result_ref='workspace-result:sha256:'+documents.digest(body)
                    # Handoff preparation ends this conversation job. Any later
                    # media task has its own lifetime and explicit user action.
                    terminal = JobState.PARTIAL if result.get('state') == 'paused' else JobState.SUCCEEDED
                    self.control.complete_worker(jid, expected_revision=job.job_revision, lease_token=lease,
                        new_state=terminal, result_ref=result_ref, changed_at=self.now())
                return
            with self.lock:
                job=self.control.show(jid)
                if job.state is JobState.CANCEL_REQUESTED:
                    self.control.settle_cancelled(jid,expected_revision=job.job_revision,lease_token=lease,changed_at=self.now())
                    return
                body=encode(result)
                library._write_atomic_text(self.path(jid,'result'),body)
                self.control.complete_worker(jid,expected_revision=job.job_revision,lease_token=lease,new_state=JobState.SUCCEEDED,
                    result_ref='workspace-result:sha256:'+documents.digest(body),changed_at=self.now())
        except Exception as exc:
            # Do not retain request headers, API keys, exception repr or raw HTTP bodies.
            message=str(exc) if isinstance(exc,(ValueError,RuntimeError,library.RevisionConflict)) else '处理遇到本地错误，已完成步骤和请求保留，未自动重试'
            body=dict(message=message,partial=self.partial.get(jid,''))
            try:
                library._write_atomic_text(self.path(jid,'error'),encode(body))
                if store is not None:
                    store.append(cid,'status',message,body,message_id=jid+'-status')
            except (OSError,RuntimeError,ValueError):
                pass
            with self.lock:
                job=self.control.show(jid)
                if job.state is JobState.CANCEL_REQUESTED:
                    self.control.settle_cancelled(jid,expected_revision=job.job_revision,lease_token=lease,changed_at=self.now())
                elif job.state not in TERMINAL_STATES:
                    self.control.complete_worker(jid,expected_revision=job.job_revision,lease_token=lease,new_state=JobState.FAILED,result_ref=None,changed_at=self.now())
        finally:
            self.partial.pop(jid,None)
            self.partial_saved_at.pop(jid,None)
            self.phases.pop(jid,None)

    def _prepare_reply(self, store, request, result):
        from .skills import validate_loaded
        validate_loaded(request)
        from .processing import validate_replacement
        from .service import WorkspaceService
        if any(c.get('name') == 'resolve_proposal' for c in result['calls']):
            return self._resolve_proposal(store, request, result)
        refs = {r['id']: r for r in request['references']}
        doc, operations, explanations = request['document'], [], []
        if not isinstance(result.get('text'), str) or not isinstance(result.get('calls'), list):
            raise ValueError('模型结果格式不完整，原始回复已保留')
        for call in result['calls']:
            if call.get('name') != 'propose_edits':
                raise ValueError('模型请求了未开放的工具，未执行')
            value = call.get('arguments')
            if (type(value) is not dict or set(value) != {'explanation', 'edits'}
                    or not isinstance(value['explanation'], str)
                    or type(value['edits']) is not list or not 1 <= len(value['edits']) <= 100):
                raise ValueError('修改建议格式不完整，模型原始结果已保留')
            for edit in value['edits']:
                if type(edit) is not dict or set(edit) != {'target', 'action', 'markdown'}:
                    raise ValueError('模型修改字段无效')
                target = edit['target']
                if target == 'current_document' and not refs:
                    ref = dict(start=0, end=len(doc['text'].encode()), before_hash=doc['content_hash'])
                elif isinstance(target, str) and target in refs:
                    ref = refs[target]
                else:
                    raise ValueError('模型指向了引用范围以外的内容')
                validate_replacement(doc['text'].encode()[ref['start']:ref['end']].decode(),
                                     edit, request.get('options') or {})
                if edit['action'] == 'append' and target in refs:
                    original = doc['text'].encode()[ref['start']:ref['end']].decode()
                    edit = dict(edit, action='replace', markdown=original + '\n\n' + edit['markdown'])
                append = edit['action'] == 'append'
                before_markdown = '' if append else doc['text'].encode()[ref['start']:ref['end']].decode()
                operations.append(dict(id=uuid4().hex, reference_id=target,
                    label_number=ref.get('label_number'), action=edit['action'],
                    start=len(doc['text'].encode()) if append else ref['start'],
                    end=len(doc['text'].encode()) if append else ref['end'],
                    before_hash=documents.digest('') if append else ref['before_hash'],
                    before_markdown=before_markdown, markdown=edit['markdown']))
            explanations.append(value['explanation'])
        # Validate every call before any proposal is visible. Combining calls also
        # catches overlapping edits across separate tool calls in the same reply.
        proposals, conflict = [], False
        if operations:
            documents.apply_operations(doc['text'], operations)
            try:
                proposals.append(WorkspaceService(store)._proposal_value(
                    request['conversation_id'], uuid4().hex, doc['revision'], doc['content_hash'], operations))
            except library.RevisionConflict:
                conflict = True
                explanations.append('正文已有新修改。以下建议保留供参考，未应用：')
                explanations.extend(op['markdown'] for op in operations)
            except FileNotFoundError:
                conflict = True
                explanations.append('目标文稿已移入最近删除或暂时不可用。以下建议已保留，恢复文稿后请重新核对：')
                explanations.extend(op['markdown'] for op in operations)
        reply = '\n\n'.join([result['text'].strip(), *explanations]).strip() or ('已整理好修改方案。' if proposals else '本轮处理结束，结果见上方记录。')
        return proposals, reply, conflict

    def _resolve_proposal(self, store, request, result):
        """A model can select a prior proposal; it cannot invent approved text."""
        from .service import WorkspaceService
        if len(result['calls']) != 1:
            raise ValueError('一次只能处理一个已有方案，不能同时生成或执行其他动作')
        value = result['calls'][0].get('arguments')
        if (type(value) is not dict or set(value) != {'proposal_id','decision','operation_ids'}
                or value['decision'] not in {'apply','discard'}
                or type(value['operation_ids']) is not list or not value['operation_ids']
                or not all(isinstance(i,str) for i in value['operation_ids'])
                or len(set(value['operation_ids'])) != len(value['operation_ids'])):
            raise ValueError('方案决定格式无效，正文未改动')
        frozen = next((p for p in request.get('pending',[]) if p['id'] == value['proposal_id']), None)
        if frozen is None or frozen['conversation_id'] != request['conversation_id'] or frozen['document_id'] != request['document']['id']:
            raise ValueError('只能处理本会话此前展示的方案，正文未改动')
        selected = [o for o in frozen['operations'] if o['id'] in value['operation_ids'] and o['state']=='pending']
        if len(selected) != len(value['operation_ids']):
            raise ValueError('指定条目已处理或不在本次方案内')
        if store.proposal(frozen['id']) != frozen:
            raise library.RevisionConflict('方案已有变化，请核对最新方案后回复')
        service = WorkspaceService(store)
        if value['decision'] == 'discard':
            for op in frozen['operations']:
                if op['id'] in value['operation_ids']:
                    op['state'] = 'discarded'
            frozen['state'] = 'partly_applied' if any(o['state']=='pending' for o in frozen['operations']) else 'discarded'
            store.proposal(frozen['id'], frozen)
            result['decision_receipt'] = dict(decision='discard', proposal_id=frozen['id'], operation_ids=value['operation_ids'])
            return [], '已放弃你指定的建议，正文保持原样。可以继续告诉我你的想法。', False
        receipt = service.proposal_apply(frozen['id'], value['operation_ids'], 'decision-'+request['request_id'])
        result['decision_receipt'] = dict(decision='apply', proposal_id=frozen['id'], **receipt)
        remaining = any(o['state']=='pending' for o in receipt['proposal']['operations'])
        return [], f'已按你的回复写入 {len(selected)} 处内容并保存。'+('其余建议仍等你决定。' if remaining else '你可以继续调整，也可以从修改记录恢复。'), False

    def _edit_preview(self, store, request, call):
        if request.get('options', {}).get('read_only'):
            raise ValueError('本轮仅讨论，未执行文稿写入')
        normalized = dict(name='propose_edits', arguments=call.get('arguments'))
        proposals, _, conflict = self._prepare_reply(store, request, dict(text='', calls=[normalized]))
        if conflict or len(proposals) != 1:
            raise library.RevisionConflict('正文版本或引用已有变化，未覆盖，请重新读取当前内容')
        return proposals[0]

    def _execute_edit(self, store, request, call):
        from .service import WorkspaceService
        proposal = self._edit_preview(store, request, call)
        before = request['document']
        operations = proposal['operations']
        receipt = WorkspaceService(store).edit_document(before['id'], before['revision'], before['content_hash'],
            operations, 'edit-' + documents.digest(request['request_id'] + ':' + call['id'])[:48])
        doc = documents.snapshot(before['id'])
        refs = []
        for ref in request['references']:
            shift = sum(len(op['markdown'].encode()) - (op['end'] - op['start']) for op in operations
                        if op['action'] == 'replace' and op['end'] <= ref['start'])
            start = ref['start'] + shift
            edited = next((op for op in operations if op.get('reference_id') == ref['id']), None)
            end = start + (len(edited['markdown'].encode()) if edited else ref['end'] - ref['start'])
            text = doc['text'].encode()[start:end].decode()
            refs.append(dict(ref, start=start, end=end, markdown=text, before_hash=documents.digest(text),
                             revision=doc['revision'], content_hash=doc['content_hash']))
        request.update(document=doc, references=refs)
        return dict(state='completed', message=f'已修改并保存 {len(operations)} 处内容。', label='修改文稿',
                    receipt=receipt, references=refs, changes=[dict(before=op.get('before_markdown',''), after=op['markdown'],
                    action=op['action']) for op in operations], target=tools.target(request))

    def _generate_turn(self, store, jid, request, projection, system, tool_support, cancelled):
        messages = list(projection['messages'])
        schemas = context.tool_schemas(request.get('options')) if tool_support else []
        steps, previous_error, seen_ids = 0, None, set()
        for round_number in range(tools.MAX_STEPS + 1):
            schemas = context.tool_schemas(request.get('options'), request.get('loaded_skills', [])) if tool_support else []
            if cancelled():
                raise model_adapter.ModelInterrupted('已停止，未启动后续工具')
            if context.estimate(system) + context.estimate(messages) + context.estimate(schemas) > projection['input_limit']:
                raise ValueError('工具记录已接近模型容量，本轮停在这里；完整结果保留，可继续对话或压缩')
            response = self.generate(request['connection'],system,messages,tools=schemas,
                on_text=lambda text:self.record_partial(jid,text),
                on_phase=lambda phase:self.record_phase(jid,phase),cancelled=cancelled)
            library._write_atomic_text(self.path(jid,f'round-{round_number}'),encode(response))
            # Keep the conventional raw-response location even when validation fails.
            library._write_atomic_text(self.path(jid,'response'),encode(response))
            if cancelled():
                raise model_adapter.ModelInterrupted('已停止，迟到工具请求未执行')
            calls = response.get('calls')
            if not isinstance(calls,list) or not isinstance(response.get('text'),str):
                raise ValueError('模型结果格式不完整，原始回复已保留')
            if not calls:
                self.record_phase(jid,'finalizing')
                return response
            self.record_phase(jid,'processing')
            if not tool_support:
                raise ValueError('当前连接未开放工具调用，异常工具回复已保留；未执行或发布修改')
            if steps + len(calls) > tools.MAX_STEPS:
                raise ValueError('本轮已达到 8 个工具步骤，进度已保留，请核对后继续')
            edit_calls = [c for c in calls if c.get('name') in {'propose_edits','resolve_proposal'}]
            if request.get('options', {}).get('read_only') and any(c.get('name') in {'propose_edits','resolve_proposal','edit_document','create_document'} for c in calls):
                raise ValueError('本轮仅讨论，未执行文稿写入')
            if edit_calls:
                # Proposed edits are terminal structured output, published as one
                # validated batch. They never execute alongside a tool effect.
                if len(edit_calls) != len(calls):
                    raise ValueError('修改建议不能和其他工具混在同一步，未执行这组请求')
                return response
            # Validate the entire batch before a single read/handoff is performed.
            ids = [c.get('id') for c in calls]
            if any(not isinstance(i,str) or not i or len(i)>200 for i in ids) or len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                raise ValueError('工具调用编号重复或无效，未重复执行')
            for call in calls:
                if call.get('name') not in {s['name'] for s in schemas}:
                    raise ValueError('此工具尚未开放，请先加载相应技能')
                if call.get('name') == 'edit_document':
                    self._edit_preview(store, request, call)
                else:
                    tools.validate(call)
            # An incomplete assistant envelope must not produce orphan results.
            tools.exchange(request['connection'], response, [dict(state='completed') for _ in calls])
            results, pause = [], False
            for call in calls:
                if cancelled():
                    raise model_adapter.ModelInterrupted('已停止，未启动后续工具')
                if pause:
                    results.append(dict(state='paused',message='前面的步骤连续遇到同一问题，此步骤未执行'))
                    continue
                try:
                    from .skills import validate_loaded
                    validate_loaded(request)
                    if call['name'] == 'edit_document':
                        from .skills import guard_loaded
                        with self.lock, library._transaction(), guard_loaded(request):
                            if cancelled():
                                raise model_adapter.ModelInterrupted('已停止，未写入正文')
                            result = self._execute_edit(store, request, call)
                    else:
                        with self.lock:
                            if cancelled():
                                raise model_adapter.ModelInterrupted('已停止，未执行后续工具')
                            result = tools.execute(call, request, task_reader=self.task_reader)
                except (ValueError, library.RevisionConflict, FileNotFoundError) as exc:
                    result = dict(state='error',code='REVISION_CONFLICT' if isinstance(exc, library.RevisionConflict) else 'INVALID_TOOL_INPUT',message=str(exc))
                fingerprint = (result.get('code'),result.get('message')) if result['state'] in {'error','unavailable'} else None
                pause = pause or (fingerprint is not None and fingerprint == previous_error)
                previous_error = fingerprint
                results.append(result)
            steps += len(calls)
            seen_ids.update(ids)
            # One durable row contains the entire exchange, so paging, checkpoint
            # boundaries and model switches cannot split a call from its result.
            content = '\n'.join([response['text'].strip(),*[r['message'] for r in results]]).strip()
            payload = dict(job_id=jid,round=round_number,calls=calls,results=results,
                           connection_id=request['connection']['id'],assistant_text=response['text'])
            with self.lock:
                if cancelled():
                    raise model_adapter.ModelInterrupted('已停止，未发布迟到工具结果')
                store.append(request['conversation_id'],'tool',content,payload,
                             message_id=f'{jid}-tools-{round_number}')
            if pause or any(r['state']=='needs_input' for r in results):
                message = '同一问题连续出现两次，本轮已暂停。已完成的步骤保留。' if pause else '请从上方任务入口完成所需选择，当前文稿和会话已保留。'
                return dict(text=message,calls=[],usage=response.get('usage',{}),state='paused' if pause else 'needs_input')
            messages.extend(tools.exchange(request['connection'],response,results))
            if any(r.get('receipt') for r in results):
                system = context.projection(store, request['conversation_id'], document=request['document'],
                    references=request['references'], connection=request['connection'], options=request.get('options'),
                    skill_entries=request.get('skills'))['system']
        raise ValueError('工具步骤达到上限，进度保留')

    def show(self,job_id):
        self.path(job_id,'input')
        job=self.control.show(job_id)
        if job.job_kind!='workspace_conversation':
            raise ValueError('任务类型不匹配')
        if job.state is JobState.CANCEL_REQUESTED:
            phase='stopping'
        elif job.state is JobState.SUCCEEDED:
            phase='complete'
        elif job.state is JobState.NEEDS_INPUT:
            phase='awaiting_input'
        elif job.state is JobState.PARTIAL:
            phase='paused'
        elif job.state is JobState.FAILED:
            phase='error'
        elif job.state is JobState.CANCELLED:
            phase='stopped'
        elif job.state is JobState.RECOVERY_REQUIRED:
            phase='interrupted'
        else:
            with self.lock:
                phase=self.phases.get(job_id,'preparing')
        response=dict(job_id=job_id,state=job.state.value,phase=phase,
                      conversation_id=job.frozen_policy['conversation_id'],partial=self.partial.get(job_id,''))
        if job.state in {JobState.SUCCEEDED, JobState.PARTIAL, JobState.NEEDS_INPUT}:
            raw=self.path(job_id,'result').read_text()
            if job.result_ref!='workspace-result:sha256:'+documents.digest(raw):
                raise RuntimeError('结果文件已变化，原记录保留')
            response['result']=json.loads(raw)
            if job.state is JobState.SUCCEEDED and response['result'].get('state') == 'needs_input':
                response['phase']='awaiting_input'
        if job.state is JobState.RECOVERY_REQUIRED:
            response['message']='服务中断，云端是否完成无法确认，未自动重发'
        if job.state is JobState.FAILED and self.path(job_id,'error').exists():
            response['message']=json.loads(self.path(job_id,'error').read_text())['message']
        return response

    def runs(self, conversation_id):
        identity(conversation_id)
        return [self.show(j.job_id) for j in self.control.list()
                if j.job_kind=='workspace_conversation' and j.frozen_policy.get('conversation_id')==conversation_id][-50:]

    def cancel(self,job_id):
        with self.lock:
            job=self.control.show(job_id)
            if job.job_kind!='workspace_conversation':
                raise ValueError('任务类型不匹配')
            if job.state not in TERMINAL_STATES:
                self.control.cancel(job_id,expected_revision=job.job_revision,changed_at=self.now())
        return self.show(job_id)
