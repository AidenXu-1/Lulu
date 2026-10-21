"""Trusted mouse events against settings assets; all downloads/bridge are fake."""
import json
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest


def run_pointer_cases(view, js, tick, bridge, fixture):
    view.resize(1448, 917)
    tick(60)
    from pathlib import Path
    information = json.loads((Path(__file__).resolve().parents[1]/'src/ui/model_settings/information.json').read_text())
    base = fixture['localModels']['extract'][0]
    fixture['localModels']['extract'] = [dict(base, id=id, name=name, default=id=='whisper', resumable=1024, presentation=information['local'][id])
        for id, name in [('sensevoice', 'SenseVoice Q8'), ('whisper', 'Whisper'),
                         ('whisper4', 'Whisper 4bit'), ('moss', 'MOSS Q5')]]
    fixture['jobs'] = {'sensevoice': dict(status='running', paused=False, progress=12, message='下载 12%')}
    bridge.snapshot.emit(json.dumps(fixture)); tick(30)
    count = len(bridge.calls)
    js("window.pointerAudit=[];document.addEventListener('click',e=>{if(e.target.closest('[data-local]'))pointerAudit.push({trusted:e.isTrusted,id:e.target.closest('[data-local]').dataset.local});},true)")

    def show(purpose, choice):
        js('window.luluModels.command("purpose",'+json.dumps(json.dumps(dict(
            purpose=purpose, localMode='local', choice=choice)))+')')
        tick(25)

    def press(selector):
        coords = json.loads(js('JSON.stringify((()=>{const el=document.querySelector('+json.dumps(selector)+');window.heldButton=el;const r=el.getBoundingClientRect();return [Math.floor(r.x+r.width/2),Math.floor(r.y+r.height/2)];})())'))
        target = view.focusProxy() or view
        target.setFocus(); QTest.mouseMove(target, QPoint(*coords)); tick(15)
        QTest.mousePress(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(*coords)); tick(25)
        return target, QPoint(*coords)

    def release(target, point):
        QTest.mouseRelease(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point); tick(40)

    cases = 0
    for purpose in ('extract', 'voice'):
        for structural in (False, True):
            for number in range(5):
                fixture['jobs']['sensevoice'].update(status='running', paused=False, progress=12, message='下载 12%')
                bridge.snapshot.emit(json.dumps(fixture)); tick(20)
                show(purpose, 'sensevoice' if purpose=='extract' else 'qwen_tts')
                selected = 'moss' if purpose=='extract' else 'voxcpm2'
                target, point = press('[data-local="'+selected+'"]')
                fixture['jobs']['sensevoice'].update(progress=73, message='下载 73%')
                if structural:
                    fixture['jobs']['sensevoice'].update(status='paused', paused=True)
                bridge.snapshot.emit(json.dumps(fixture)); tick(25)
                assert js('window.heldButton.isConnected'), (purpose, structural, 'button replaced during press')
                if purpose=='extract' and not structural:
                    assert js('document.querySelector(".install-progress progress").value') == 73
                    assert js('document.querySelector(".progress-heading h3").textContent') == '下载 73%'
                release(target, point)
                assert js('document.querySelector(".rail-model[aria-pressed=true]").dataset.local') == selected
                cases += 1

    # Byte count changes only affect whether resume is available, not navigation.
    show('extract', 'sensevoice')
    fixture['jobs']['sensevoice'].update(status='running', paused=False)
    bridge.snapshot.emit(json.dumps(fixture)); tick(25)
    js('window.progressNode=document.querySelector("progress");window.pauseNode=document.querySelector("[data-local-action=pause]")')
    for value in (-1, 49, 50):
        fixture['jobs']['sensevoice'].update(progress=value, message='进度 '+str(value))
        fixture['localModels']['extract'][0]['resumable'] = 1024 + max(value, 0)
        bridge.snapshot.emit(json.dumps(fixture)); tick(25)
        if value == -1:
            assert js('!document.querySelector("progress").hasAttribute("value")')
        else:
            assert js('document.querySelector("progress").value') == value
    assert js('window.progressNode===document.querySelector("progress") && window.pauseNode===document.querySelector("[data-local-action=pause]")')

    # Qwen feature progress uses the same stable in-place path.
    fixture['jobs']['feature:clone'] = dict(status='running', paused=False, progress=20, message='声音功能 20%')
    bridge.snapshot.emit(json.dumps(fixture)); tick(25); show('voice', 'qwen_tts')
    js('window.featureNode=document.querySelector(".feature-progress progress")')
    fixture['jobs']['feature:clone'].update(progress=80, message='声音功能 80%')
    bridge.snapshot.emit(json.dumps(fixture)); tick(25)
    assert js('window.featureNode===document.querySelector(".feature-progress progress") && window.featureNode.value===80')
    assert js('document.querySelector(".feature-progress").title') == '声音功能 80%'
    assert len(bridge.calls) == count, 'navigation or progress must never control a download'
    events = json.loads(js('JSON.stringify(window.pointerAudit)'))
    assert len(events) == cases and all(e['trusted'] for e in events)

    # Losing the pointer or window focus must not leave structural updates stuck.
    for event in ('pointercancel', 'blur'):
        fixture['jobs']['sensevoice'].update(status='running', paused=False)
        bridge.snapshot.emit(json.dumps(fixture)); tick(25); show('extract', 'sensevoice')
        js("window.releaseProbe=document.querySelector('[data-local=sensevoice]');document.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
        fixture['jobs']['sensevoice'].update(status='paused', paused=True)
        bridge.snapshot.emit(json.dumps(fixture)); tick(25)
        assert js('window.releaseProbe.isConnected')
        js("document.dispatchEvent(new PointerEvent('pointercancel',{bubbles:true}))" if event=='pointercancel' else "window.dispatchEvent(new Event('blur'))")
        tick(25)
        assert js('Boolean(document.querySelector("[data-local-action=resume]"))')

    # Terminal states still refresh; pause/resume/cancel remain explicit requests.
    show('extract', 'sensevoice')
    fixture['jobs']['sensevoice'].update(status='paused', paused=True)
    bridge.snapshot.emit(json.dumps(fixture)); tick(25)
    js('document.querySelector("[data-local-action=resume]").click()'); tick(25)
    assert bridge.calls[-1][1:] == ('local_action', {'id':'sensevoice', 'action':'resume'})
    fixture['jobs']['sensevoice'].update(status='running', paused=False)
    bridge.snapshot.emit(json.dumps(fixture)); tick(25)
    js('document.querySelector("[data-local-action=pause]").click()'); tick(25)
    assert bridge.calls[-1][1:] == ('local_action', {'id':'sensevoice', 'action':'pause'})
    js('document.querySelector("[data-local-action=cancel]").click()'); tick(25)
    assert bridge.calls[-1][1:] == ('local_action', {'id':'sensevoice', 'action':'cancel'})
    fixture['jobs'].pop('sensevoice')
    fixture['localModels']['extract'][0].update(installed=True, ready=True)
    bridge.snapshot.emit(json.dumps(fixture)); tick(25)
    assert js('document.querySelector(".detail-top .status").textContent') == '已安装'
    assert not js('Boolean(document.querySelector(".install-progress"))')
    return dict(clicks=cases, pages=2, native_navigation_requests=0, progress_and_actions=True)
