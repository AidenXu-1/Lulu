"""Product layout is a capability, independent of momentary Core readiness."""
import json
import unittest
from unittest.mock import patch, PropertyMock
from tests.test_extraction_workspace import ExtractionWorkspaceFixture
from src.ui.extraction.batches import WorkspaceBatches


class ExtractionLayoutContractTests(ExtractionWorkspaceFixture,unittest.TestCase):
    def setUp(self):
        self.capability=patch.object(WorkspaceBatches,'supported',new_callable=PropertyMock,create=True,return_value=True)
        self.capability.start()
        super().setUp();self.widget.timer.stop()

    def tearDown(self):
        super().tearDown();self.capability.stop()

    def test_cold_product_with_core_unready_never_exposes_legacy_header(self):
        self.assertFalse(self.widget.batches.enabled)
        self.assertEqual(self.js('getComputedStyle(document.querySelector(".pagehead>button")).display'),'none')
        self.assertEqual(self.js('getComputedStyle(document.querySelector("#model-picker")).display'),'none')

    def test_readiness_changes_cannot_change_header_or_card_geometry(self):
        self.add(1)
        data=json.loads(self.widget._last)
        self.js('window.layoutSamples=[];window.layoutNodes=[]')
        for enabled in (False,True,False,False,True,False):
            data['batchesEnabled']=enabled;data['batchesSupported']=True
            self.js('receive('+json.dumps(data,ensure_ascii=False)+''');layoutSamples.push((()=>{
                const n=document.querySelector('[data-job]');layoutNodes.push(n);
                return {w:n.offsetWidth,h:n.offsetHeight,header:getComputedStyle(document.querySelector('.pagehead>button')).display}
            })())''')
            self.tick()
        samples=json.loads(self.js('JSON.stringify(layoutSamples)'))
        self.assertEqual(len({(s['w'],s['h']) for s in samples}),1,samples)
        self.assertTrue(all(s['header']=='none' for s in samples),samples)
        self.assertTrue(self.js('layoutNodes.every(n=>n===layoutNodes[0])'))
