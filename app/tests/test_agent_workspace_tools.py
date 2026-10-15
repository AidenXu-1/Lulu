import json
import unittest
from unittest.mock import patch

from tests import test_agent_workspace_conversations as conversations
from src.core.agent_workspace import context, documents, tools
from src.core.agent_workspace.service import WorkspaceService


def reply(name, arguments, cid='call1', protocol='openai'):
    call=dict(id=cid,name=name,arguments=arguments)
    if protocol=='anthropic':
        assistant=dict(role='assistant',content=[dict(type='thinking',thinking='private',signature='signed'),
            dict(type='tool_use',id=cid,name=name,input=arguments)])
    else:
        assistant=dict(role='assistant',content=None,reasoning_content='private',
            tool_calls=[dict(id=cid,type='function',function=dict(name=name,arguments=json.dumps(arguments)))])
    return dict(text='',calls=[call],usage={},assistant=assistant)


class ToolsTests(unittest.TestCase):
    setUp=conversations.ConversationsTests.setUp
    runner=conversations.ConversationsTests.runner
    wait=conversations.ConversationsTests.wait


if __name__=='__main__':unittest.main()
