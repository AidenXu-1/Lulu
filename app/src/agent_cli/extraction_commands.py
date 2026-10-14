"""Closed argument grammar for independent link extraction."""
from ..core.transcription_job_application import LocalTranscriptionJobError


def execute(args, runtime):
    def invalid():
        raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'EXTRACT_OPTIONS_INVALID')
    if len(args) < 9 or args[1] not in {'prepare', 'run'} or len(args[3:]) % 2:
        invalid()
    values = {}
    for key, value in zip(args[3::2], args[4::2]):
        if key in values:
            invalid()
        values[key] = value
    required = {'--outputs', '--destination', '--output'}
    if args[1] == 'run':
        required |= {'--plan-digest', '--request-id'}
    if set(values) != required or values['--output'] != 'json':
        invalid()
    options = dict(source=args[2], outputs=values['--outputs'], destination=values['--destination'])
    service = runtime.extraction()
    if args[1] == 'prepare':
        return service.prepare(**options)
    return service.run(**options, plan_digest=values['--plan-digest'], request_id=values['--request-id'])
