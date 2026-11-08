"""Asynchronous GUI adapter for the same per-work service as the formal CLI."""
from copy import deepcopy
from concurrent.futures import Future
from pathlib import Path
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QFileSystemWatcher, QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog


class Request(QThread):
    result = Signal(object, str)

    def __init__(self, dispatcher, operation, arguments):
        super().__init__()
        self.dispatcher, self.operation, self.arguments = dispatcher, operation, arguments

    def run(self):
        try:
            if self.operation=='batch_stage' and self.arguments.get('items'):
                import json
                import tempfile
                import hashlib
                payload=''.join(json.dumps(i,ensure_ascii=False)+'\n' for i in self.arguments['items']).encode()
                if len(payload)>512*1024:
                    with tempfile.TemporaryDirectory(prefix='lulu-work-list-') as directory:
                        path=Path(directory).resolve()/'works.jsonl'
                        with path.open('xb') as stream:
                            import os
                            os.fchmod(stream.fileno(),0o600);stream.write(payload)
                        args={k:v for k,v in self.arguments.items() if k!='items'}
                        args.update(items_file=str(path),items_sha256=hashlib.sha256(payload).hexdigest())
                        self.result.emit(self.dispatcher(self.operation,**args),'')
                        return
            self.result.emit(self.dispatcher(self.operation, **self.arguments), '')
        except (ValueError, OSError, RuntimeError) as exc:
            self.result.emit(None, str(exc)[:1500])
        except Exception:
            self.result.emit(None, '操作结果未确认，请刷新核对；未自动再次发送')


class WorkspaceBatches(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.widget, self.main = widget, widget.main
        self.worker = None
        self.commands, self.reads = [], []
        self.views, self.pages, self.selections = {}, {}, {}
        self._at = 0
        self._retry_at = 0
        self._last_full_poll = 0
        self._poll_dirty = True
        self._poll_watch = QFileSystemWatcher(self)
        self._poll_watch.directoryChanged.connect(self._invalidate_poll)
        self.closing = False
        self.draining = False
        self.close_error = ''
        self.error = ''
        self.undo = None
        self.legacy_count = 0

    @property
    def supported(self):
        # Product capability is fixed even while Core is starting or unavailable.
        # Readiness may disable an operation; it must never choose the UI layout.
        return (callable(getattr(self.main, '_workspace_batch_dispatcher', None))
                or getattr(sys,'frozen',False) and Path(sys.executable).name=='Lulu')

    @property
    def enabled(self):
        if callable(getattr(self.main, '_workspace_batch_dispatcher', None)):
            return True
        # A routine shared-record read does not revoke an established connection.
        # Initial startup, installation and actual failures retain the safe intake.
        return (getattr(sys,'frozen',False) and Path(sys.executable).name=='Lulu'
                and getattr(getattr(self.main,'_task_queue_store',None),'async_write',False) is True
                and not (getattr(self.main,'_task_queue_startup_pending',False)
                         and getattr(self.main,'_task_queue_initial_connection',False))
                and not any(getattr(self.main,key,False) for key in (
                    '_install_preparation_pending','_install_preparation_error',
                    '_task_queue_store_failed')))

    def shutdown(self):
        self.closing = True
        paths = self._poll_watch.directories()
        if paths:
            self._poll_watch.removePaths(paths)
        for entry in self.commands + self.reads:
            entry[5].cancel()
        self.commands.clear(); self.reads.clear()

    def prepare_close(self):
        """Finish accepted writes through the GUI event loop before discarding reads."""
        self.draining = True
        for entry in self.reads:
            entry[5].cancel()
        self.reads.clear()
        return self.worker is None and not self.commands

    def _notice(self, message):
        if not self.closing:
            self.widget.bridge.notice.emit(message)

    def _submit(self, operation, arguments, callback, *, mutation=False, control_key=None):
        if self.closing or self.draining or not self.enabled:
            raise RuntimeError('任务服务尚未就绪，请稍后重试；输入仍保留')
        completion = Future()
        entry = (operation, arguments, callback, mutation, control_key, completion)
        (self.commands if mutation else self.reads).append(entry)
        self._pump()
        return completion

    def _pump(self):
        if self.worker is not None or self.closing:
            return
        queue = self.commands if self.commands else self.reads
        if not queue:
            return
        operation, arguments, callback, mutation, control_key, completion = queue.pop(0)
        try:
            if callable(arguments):
                arguments = arguments()
        except Exception as exc:
            message = (str(exc) if isinstance(exc,(ValueError,OSError,RuntimeError)) else
                       '任务选择已变化，请刷新后重试')
            self._notice(message)
            if self.draining and mutation:
                self.close_error = message
            completion.set_exception(RuntimeError(message))
            self.widget.publish(force=True)
            QTimer.singleShot(0, self._pump)
            return
        dispatcher = getattr(self.main, '_workspace_batch_dispatcher', None)
        if dispatcher is None:
            from ...core.agent_workspace.client import dispatch
            dispatcher = dispatch
        worker = self.worker = Request(dispatcher, operation, arguments)
        worker.mutation = mutation
        worker.control_key = control_key
        def result(value, error):
            if self.closing:
                completion.cancel()
                return
            if self.draining and not mutation:
                completion.cancel()
                return
            if not error:
                try:
                    callback(value)
                except Exception as exc:
                    error = (str(exc) if isinstance(exc,(ValueError,OSError,RuntimeError)) else
                             '任务返回内容未能核对，请刷新检查；未自动再次发送')
            if error:
                self.error = error
                if mutation:
                    self._notice(error)
                    if self.draining:
                        self.close_error = error
                self._retry_at = time.monotonic()+5
            else:
                self.error = ''
                self._retry_at = 0
            self.widget.publish(force=True)
            # A browser acknowledgment means the saved state has arrived, not
            # merely that its request was placed in our in-memory queue.
            if error:
                completion.set_exception(RuntimeError(error))
            else:
                completion.set_result(None)
        def settled():
            self.worker = None
            worker.deleteLater()
            if not self.closing:
                self._pump()
                self.widget.publish(force=True)
        worker.result.connect(result)
        worker.finished.connect(settled)
        worker.start()

    def _invalidate_poll(self, _path):
        self._poll_dirty = True

    def _watch_batches(self):
        from ...core import library
        path = str(library.LIBRARY_DIR / '.workspace' / 'batches')
        watched = self._poll_watch.directories()
        if watched == [path]:
            return True
        if watched:
            self._poll_watch.removePaths(watched)
        self._poll_dirty = True
        # Core creates this directory. Failure to watch only disables the idle
        # optimization; it never makes records trusted or changes verification.
        return self._poll_watch.addPath(path)

    def refresh(self, force=False):
        active = any(any(view.get('counts',{}).get(state,0) for state in ('queued','running','settling'))
                     for view in self.views.values())
        interval = 1 if active else 3 if self.widget.isVisible() else 5
        if (not self.enabled or self.closing or self.draining or self.worker is not None or self.reads
                or self.commands or not force and (time.monotonic()-self._at < interval
                                                   or time.monotonic()<self._retry_at)):
            return
        self._at = now = time.monotonic()
        watching = self._watch_batches()
        # Batch/state writes are atomic replacements in this directory. Watch
        # events only request a fresh, fully verified Core read. Keep a bounded
        # fallback for lost events and out-of-band edits, even while hidden.
        if (watching and not active and not force and not self.error
                and not self._poll_dirty and now-self._last_full_poll < 30):
            return
        self._poll_dirty = False
        active=set(); existing=set(self.views); requested=dict(self.pages)
        legacy_count=0; cursor=''
        def accept(value):
            nonlocal legacy_count,cursor
            rows = value.get('batches', [])
            from ...core.engines.cloud_requests import cloud_ids
            legacy_count+=sum(not r.get('workspace') and r.get('engine') in cloud_ids() for r in rows)
            for row in rows:
                if not row.get('workspace'):
                    continue
                bid = row['batch_id']; active.add(bid)
                # Publish each batch only with its matching member details. A
                # summary joined to stale members exposes contradictory actions.
                if row.get('details_deferred'):
                    self.page(bid,self.pages.get(bid,row['offset']),total=row['total'])
                    continue
                if self.pages.get(bid,0)==requested.get(bid,0):
                    self.pages[bid] = row['offset']
                self._accept(row)
            next_after=value.get('next_after','')
            if next_after:
                if type(next_after) is not str or next_after<=cursor:
                    raise ValueError('任务分页未能继续，原任务保留，请刷新后核对')
                cursor=next_after
                self._submit('batch_poll',{'pages':requested,'after':cursor},accept)
                return
            self.legacy_count=legacy_count
            self._last_full_poll = now
            for bid in existing-active:
                self.views.pop(bid, None); self.pages.pop(bid, None); self.selections.pop(bid, None)
        self._submit('batch_poll', {'pages':requested}, accept)

    def _accept(self, reply):
        # Do not replace a usable card with a partial/incompatible response.
        if (type(reply) is not dict or not {'batch_id','items','workspace','counts','engine',
                'plan_hash','visible_total','total','offset'} <= reply.keys()
                or type(reply['items']) is not list or type(reply['workspace']) is not dict
                or type(reply['counts']) is not dict
                or any(type(reply[k]) is not int or reply[k]<0 for k in ('visible_total','total','offset'))
                or any(type(item) is not dict or not {'index','title','state','selection','assets','version'} <= item.keys()
                       for item in reply['items'])):
            raise ValueError('任务详情未能完整读取，原任务保留，请刷新后核对')
        bid = reply['batch_id']
        previous = self.views.get(bid)
        new_attention = (set(reply.get('attention_keys',())) - set(previous.get('attention_keys',()))
                         if previous is not None else set())
        if new_attention:
            feedback=getattr(self.main,'completion_feedback',None)
            if feedback is not None:
                feedback.needs_attention(app_is_active=self.main.isActiveWindow())
        own_visibility = reply.pop('previous_visibility', None)
        if (previous and previous.get('visibility_version') != reply.get('visibility_version')
                and previous.get('visibility_version') != own_visibility
                and self.selections.get(bid)):
            self.selections.pop(bid, None)
            self._notice('作品列表已变化，已清除原勾选，请重新选择后操作')
        changes = reply.pop('changed_items', [])
        requested = self.pages.get(bid, 0)
        mismatch = reply.get('offset', 0) != requested and bid in self.views
        if mismatch:
            if not changes:
                self._at = 0
                self.page(bid, requested)
                return
            updates = {i['index']:i for i in changes}
            reply = {**reply, 'items':[updates.get(i['index'],i) for i in self.views[bid].get('items', [])], 'offset':requested}
            self._at = 0
        self.views[bid] = reply
        selected = self.selections.get(bid, {})
        for item in changes:
            if item.get('hidden'):
                selected.pop(item['index'],None)
            elif not selected.get('all') and item['index'] in selected:
                selected[item['index']]=deepcopy(item)
        if not selected.get('all'):
            for item in reply.get('items', []):
                if item['index'] in selected:
                    selected[item['index']] = deepcopy(item)
        if mismatch:
            self.page(bid, requested)

    def page(self, bid, offset, *, total=None):
        if (bid not in self.views and total is None) or type(offset) is not int or offset < 0:
            raise ValueError('任务分页已变化，请刷新')
        total = self.views[bid]['total'] if total is None else total
        offset = min(offset, max(0, total-1)) // 25 * 25
        self.pages[bid] = offset
        # Supersede only unsent reads; mutations are never dropped or retried.
        self.reads = [r for r in self.reads if not (r[0]=='batch_show' and r[1].get('batch_id')==bid)]
        self._submit('batch_show', dict(batch_id=bid, offset=offset, limit=50), self._accept)

    def stage(self, items, *, name=None, group=False, folder_id=None, skipped=None, engine=None):
        from ...core.engines.providers import PRESETS
        chosen = engine or self.main.cfg.engine_name
        if chosen not in PRESETS or PRESETS[chosen].retired:
            chosen = 'local_sensevoice_q8'
        request_id = uuid4().hex
        directory = self.main.cfg.media_dir or None
        rows = [dict(row, media_directory=row.get('media_directory', directory)) for row in items]
        args = dict(request_id=request_id, items=rows, engine=chosen, folder_id=folder_id,
                    name=name, group=group, skipped=skipped or [])
        # The page paints the admitted local intent immediately. It becomes an
        # actionable task only when the durable Core reply arrives.
        import json
        preview = dict(batch_id='intake_'+request_id, _intake=True, engine=chosen,
            workspace=dict(name=name or '待添加任务',group=group,created_at=time.time()), model_locked=True,
            visible_total=len(rows),total=len(rows),offset=0,counts={},selectedCount=0,
            selection=[],selectAll=False,items=[dict(index=n,title=row.get('title') or '待添加任务',
                source=row.get('source',''),local_source=bool(row.get('local_source')) or Path(row.get('source','')).is_absolute(),
                state='staging',selection=dict(outputs=row.get('outputs',['text']),
                    prefer_subtitles=row.get('prefer_subtitles',False)),
                assets={key:dict(state='missing') for key in ('text','video','cover')})
                for n,row in enumerate(rows[:50])])
        self.widget.bridge.intake.emit(json.dumps(dict(token=request_id,batch=preview),ensure_ascii=False))
        def accepted(reply):
            reply['_intakeToken'] = request_id
            reply['_animateNew'] = True
            self.pages[reply['batch_id']] = 0
            self._accept(reply)
            self._at = 0
        try:
            completion = self._submit('batch_stage', args, accepted, mutation=True)
        except Exception:
            self.widget.bridge.intake.emit(json.dumps(dict(token=request_id,batch=None)))
            raise
        def finished(result):
            if self.closing:return
            if result.cancelled() or result.exception() is not None:
                self.widget.bridge.intake.emit(json.dumps(dict(token=request_id,batch=None)))
        completion.add_done_callback(finished)
        return completion

    def _row(self, bid, index):
        if type(bid) is not str or type(index) is not int or index < 0:
            raise ValueError('任务或作品编号无效')
        view = self.views.get(bid)
        if not view:
            raise ValueError('任务已变化，请刷新')
        item = next((i for i in view.get('items', []) if i['index']==index), None)
        if not item:
            raise ValueError('此条尚未读取，请等待列表更新')
        return view, item

    def configure(self, bid, index, key, value):
        if key not in {'text','video','cover','subs'} or type(value) is not bool:
            raise ValueError('提取选项无效')
        def arguments():
            view, item = self._row(bid, index)
            selection = deepcopy(item['selection'])
            if key == 'subs':
                changes = dict(prefer_subtitles=value)
            else:
                selected = set(selection['outputs'])
                selected.add(key) if value else selected.discard(key)
                changes = dict(outputs=[k for k in ('text','video','cover') if k in selected])
            return dict(request_id=uuid4().hex, batch_id=bid, expected_hash=view['plan_hash'],
                        changes=[dict(index=index, version=item['version'], **changes)])
        return self._submit('batch_configure', arguments, self._accept, mutation=True)

    def configure_choices(self, bid, index, outputs, prefer_subtitles):
        if (type(outputs) is not list
                or any(type(k) is not str or k not in {'text','video','cover'} for k in outputs)
                or len(outputs)!=len(set(outputs))
                or type(prefer_subtitles) is not bool):
            raise ValueError('提取选项无效')
        def arguments():
            view, item = self._row(bid, index)
            return dict(request_id=uuid4().hex, batch_id=bid, expected_hash=view['plan_hash'],
                        changes=[dict(index=index, version=item['version'], outputs=outputs,
                                      prefer_subtitles=prefer_subtitles)])
        return self._submit('batch_configure', arguments, self._accept, mutation=True)

    def set_model(self, bid, engine):
        def arguments():
            view = self.views[bid]
            return dict(request_id=uuid4().hex, batch_id=bid, expected_hash=view['plan_hash'], engine=engine)
        self._submit('batch_configure', arguments, self._accept, mutation=True)

    def new_text(self, bid, index):
        view, item = self._row(bid, index)
        from .. import dialogs
        if not dialogs.confirm(self.main, '另建文案任务',
                '原文稿和素材会保留。新任务可单独选择模型，点击开始后才会识别；使用云端会重新计费。是否创建？'):
            return
        self.stage([dict(source=item['source'],title=item['title'],
                        outputs=['text'],new_text=True,prefer_subtitles=item['selection']['prefer_subtitles'])],
                   name=item['title'],engine=view['engine'],folder_id=view['folder_id'])

    def locate(self, bid, index):
        view, item = self._row(bid, index)
        reference = item.get('existing_task') or dict(batch_id=bid,index=item.get('duplicate_of'))
        if type(reference.get('index')) is not int:
            raise ValueError('原任务引用不可用，记录仍保留')
        def accept(reply):
            self.pages[reply['batch_id']]=reply['offset']
            reply['focus_token']=uuid4().hex
            self._accept(reply)
        self._submit('batch_show',dict(batch_id=reference['batch_id'],locate_index=reference['index'],limit=50),accept)

    def folder(self, bid, index):
        view, item = self._row(bid, index)
        path = QFileDialog.getExistingDirectory(self.widget, '选择本条视频与封面的保存位置',
                    item['selection'].get('media_directory') or self.main.cfg.media_dir)
        if not path:
            return
        # The native picker runs a nested event loop; freeze and recheck this version.
        args = dict(request_id=uuid4().hex, batch_id=bid, expected_hash=view['plan_hash'],
                    changes=[dict(index=index, version=item['version'], media_directory=path)])
        self._submit('batch_configure', args, self._accept, mutation=True)

    def select(self, bid, index, selected):
        if type(selected) is not bool:
            raise ValueError('选择无效')
        values = self.selections.setdefault(bid, {})
        if index is None:
            self.selections[bid] = {'all':True} if selected else {}
        elif selected:
            _, item = self._row(bid, index)
            if values.get('all'):
                values.pop(index, None)
            else:
                values[index] = deepcopy(item)
        elif values.get('all'):
            values[index] = False
        else:
            values.pop(index, None)

    def control(self, bid, action, indices=None, selected=False, *, confirmed=False):
        if type(bid) is not str or type(selected) is not bool or (indices is not None and
                (type(indices) is not list or any(type(i) is not int or i<0 for i in indices))):
            raise ValueError('任务选择无效')
        view = self.views.get(bid)
        if not view:
            raise ValueError('任务已变化，请刷新')
        control_index = indices[0] if indices is not None and len(indices)==1 else 'all'
        control_key = f'{bid}|{control_index}|{action}|{int(selected)}'
        chosen = None if indices is None else [self._row(bid, i)[1] for i in indices]
        if selected:
            selection = self.selections.get(bid, {})
            if selection.get('all'):
                chosen = None
            else:
                indices = [i for i in selection if type(i) is int]
                chosen = [selection[i] for i in indices]
                if not chosen:
                    raise ValueError('请先选择作品')
        from ...core.engines.cloud_requests import cloud_ids
        from .. import dialogs
        if selected and action == 'resume':
            action = 'resume_selected'
        if action in {'start','retry','resume','resume_selected','retry_unknown'}:
            cloud = view['engine'] in cloud_ids()
            possible_text = (view.get('needs_cloud_upload', cloud) if chosen is None else any(
                'text' in i['selection']['outputs'] and (i['assets']['text']['state'] not in {'ready','unknown'}
                    or action=='retry_unknown' and i['assets']['text']['state']=='unknown') for i in chosen))
            if cloud and possible_text:
                if not dialogs.confirm(self.main, '使用云端转录',
                        '只处理各条作品所选的未完成内容。需要语音识别时音频会上传至所选服务商，并按你的账户计费。是否开始？'):
                    return
            if action == 'retry_unknown' and not dialogs.confirm(self.main, '再次上传结果不明项',
                    '这些音频可能已经处理并计费，再次上传可能重复收费。仅在你已核对后继续。'):
                return
        if action == 'cancel' and chosen and any(i['state'] in {'running','settling'} for i in chosen):
            if not dialogs.confirm(self.main, '取消本条任务',
                    '已有结果保留。音频已上传时，服务商仍可能完成处理并计费。是否取消等待？'):
                return
        if not confirmed and action == 'dismiss' and chosen and any(i['state'] in {'running','settling'} for i in chosen):
            if not dialogs.confirm(self.main, '移出正在处理的作品',
                    '将停止等待并移出本批，已有文案和素材保留；服务商仍可能处理并计费。是否继续？'):
                return
        if not confirmed and action == 'dismiss' and chosen is None:
            if not dialogs.confirm(self.main, '移出本批任务',
                    '本批将移出任务列表，已有文稿和素材保留。正在上传的请求仍可能完成并计费。是否继续？'):
                return
        args = dict(request_id=uuid4().hex, batch_id=bid, expected_hash=view['plan_hash'], action=action,
                    network_consent=True, cloud_upload_consent=True, retry_unknown_confirmed=action=='retry_unknown')
        if chosen is None and view.get('visibility_version'):
            args['expected_visibility'] = view['visibility_version']
        if chosen is not None:
            args['item_indices'] = [i['index'] for i in chosen]
            args['expected_versions'] = {str(i['index']):i['version'] for i in chosen}
        elif selected:
            args['excluded_indices'] = [i for i,v in self.selections.get(bid,{}).items()
                                        if type(i) is int and v is False]
        def accept(reply):
            changed = deepcopy(reply.get('changed_items', []))
            self._accept(reply);self._at=0
            if selected or action=='dismiss' and chosen is None:
                self.selections.pop(bid,None)
            if action=='dismiss':
                self.undo = None if confirmed else dict(token=uuid4().hex, batch_id=bid, expires=time.monotonic()+6,
                                 dismissal_id=reply['dismissal_id'],count=reply['dismissed_count'])
            if action=='check' and changed:
                self._notice(changed[0].get('message') or '已核对本机结果；没有重新上传')
        return self._submit('batch_control', args, accept, mutation=True, control_key=control_key)

    def clear_visible(self, scope=None):
        """Dismiss the captured category across pages; preserve files and results."""
        if scope is not None and (type(scope) is not str or scope not in {'single', 'batch'}):
            raise ValueError('任务分类无效')
        completion = Future()
        bids = [bid for bid, view in self.views.items() if view.get('visible_total',0)
                and (scope is None or bool(view.get('workspace',{}).get('group')) == (scope=='batch'))]
        failures = []
        def next_batch():
            if not bids:
                if failures:
                    completion.set_exception(RuntimeError('部分任务未清空，仍保留在列表：'+'；'.join(failures)))
                else:
                    completion.set_result(True)
                return
            bid = bids.pop(0)
            try:
                future = self.control(bid, 'dismiss', confirmed=True)
            except Exception as exc:
                failures.append(str(exc)); next_batch(); return
            def finished(result):
                try: result.result()
                except Exception as exc: failures.append(str(exc))
                QTimer.singleShot(0,next_batch)
            future.add_done_callback(finished)
        next_batch()
        return completion

    def undo_notice(self):
        if self.undo and time.monotonic()<self.undo['expires']:
            return dict(token=self.undo['token'], count=self.undo['count'])
        return None

    def restore(self, token):
        if not self.undo_notice() or token != self.undo['token']:
            raise ValueError('撤销时间已结束，任务文件和结果仍保留')
        undo = self.undo
        args = dict(request_id=uuid4().hex, batch_id=undo['batch_id'],
                    expected_hash=self.views[undo['batch_id']]['plan_hash'], action='restore',
                    dismissal_id=undo['dismissal_id'])
        def accept(reply):
            self._accept(reply);self.undo=None;self._at=0
            self._notice('已恢复到列表，未重新启动已取消的任务')
        self._submit('batch_control', args, accept, mutation=True)

    def result(self, bid, index, key):
        _, item = self._row(bid, index)
        if key not in {'text','video','cover'}:
            raise ValueError('结果类型无效')
        asset = item['assets'][key]
        if asset['state'] != 'ready':
            raise ValueError('此项还未提取完成')
        if key == 'text':
            self.main._view_entry(asset['document_id'])
        else:
            path = Path(asset['path'])
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError('此素材已移动或不可用，原任务记录保留')
            self.main._reveal(str(path))

    def snapshot(self):
        result = []
        pending = [entry[4] for entry in self.commands if entry[4]]
        if self.worker is not None and getattr(self.worker, 'control_key', None):
            pending.append(self.worker.control_key)
        for bid, view in self.views.items():
            values = self.selections.get(bid, {})
            selection_count = (view['visible_total']-sum(v is False for k,v in values.items() if k!='all')
                               if values.get('all') else len(values))
            result.append({**view, 'pending_controls':[key for key in pending if key.startswith(bid+'|')],
                           'selection':list(k for k in values if type(k) is int),
                           'selectAll':bool(values.get('all')), 'selectedCount':selection_count})
        return result
