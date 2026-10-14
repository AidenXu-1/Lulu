"""One public workspace command registry for discovery, CLI and Core authorization."""
COMMANDS = {
    'documents.list': ('WS-01', 'list', 'read'),
    'documents.show': ('WS-02', 'snapshot', 'read'),
    'documents.create': ('WS-03', 'create_document', 'local_write'),
    'documents.save': ('WS-04', 'save', 'local_write'),
    'documents.history': ('WS-05', 'history', 'read'),
    'conversations.list': ('WS-06', 'conversations', 'read'),
    'conversations.create': ('WS-07', 'new_conversation', 'local_write'),
    'conversations.show': ('WS-08', 'conversation_snapshot', 'read'),
    'conversations.send': ('WS-09', 'send', 'cloud_request'),
    'conversations.compact': ('WS-10', 'compact', 'cloud_request'),
    'conversations.runs': ('WS-11', 'runs', 'read'),
    'conversations.status': ('WS-12', 'run_status', 'read'),
    'conversations.cancel': ('WS-13', 'cancel_run', 'local_write'),
    'conversations.delete': ('WS-30', 'delete_conversation', 'local_write'),
    'edits.plan': ('WS-14', 'proposal_plan', 'local_write'),
    'edits.apply': ('WS-15', 'proposal_apply', 'local_write'),
    'edits.discard': ('WS-16', 'proposal_discard', 'local_write'),
    'presets.list': ('WS-17', 'presets', 'read'),
    'providers.list': ('WS-18', 'connections', 'read'),
    'tools.show': ('WS-19', 'tool_handoff', 'read'),
    'documents.voice-target': ('WS-20', 'voice_target', 'read'),
    'storage.status': ('WS-21', 'storage_status', 'read'),
    'storage.backup': ('WS-22', 'backup', 'local_write'),
    'storage.restore': ('WS-23', 'restore_backup', 'local_write'),
    'tasks.prepare': ('WS-24', 'prepare_media', 'local_write'),
    'tasks.start': ('WS-25', 'start_media', 'local_inference'),
    'tasks.show': ('WS-26', 'media_status', 'read'),
    'tasks.list': ('WS-27', 'media_tasks', 'read'),
    'tasks.cancel': ('WS-28', 'cancel_media', 'local_write'),
    'tasks.recover': ('WS-29', 'recover_media', 'local_write'),
    'conversations.rename': ('WS-31', 'rename_conversation', 'local_write'),
    'conversations.archive': ('WS-32', 'archive_conversation', 'local_write'),
    'documents.edit': ('WS-33', 'edit_document', 'local_write'),
    'documents.restore': ('WS-34', 'restore', 'local_write'),
    'documents.reference': ('WS-35', 'reference', 'read'),
    'skills.list': ('WS-36', 'skills', 'read'),
    'documents.undo': ('WS-38', 'undo_edit', 'local_write'),
    'tools.catalog': ('WS-37', 'tool_catalog', 'read'),
}
COMMANDS.update({
    'skills.manage': ('WS-39', 'skill_list', 'read'),
    'skills.show': ('WS-40', 'skill_details', 'read'),
    'skills.inspect': ('WS-41', 'skill_inspect', 'local_write'),
    'skills.install': ('WS-42', 'skill_install', 'local_write'),
    'skills.uninstall': ('WS-43', 'skill_uninstall', 'local_write'),
    'skills.configure': ('WS-44', 'skill_configure', 'local_write'),
    'skills.export': ('WS-45', 'skill_export', 'local_write'),
    'skills.drafts': ('WS-46', 'skill_drafts', 'read'),
    'skills.migrate': ('WS-47', 'skill_migrate_presets', 'local_write'),
    'skills.update': ('WS-48', 'skill_update', 'local_write'),
})

CAPABILITIES=frozenset(item[0] for item in COMMANDS.values())


def discovery(activated):
    return [dict(capability_id=cap,command_id='workspace.'+name,
                 argv_template='lulu workspace '+name.replace('.',' ')+' --request <json-file> --output json',
                 state='available' if cap in activated else 'unavailable',
                 executable=cap in activated,reason_code=None if cap in activated else 'TRUSTED_RUNTIME_REQUIRED',
                 effect=effect,schema_revision=1,display_target_state='available')
            for name,(cap,_,effect) in COMMANDS.items()]
