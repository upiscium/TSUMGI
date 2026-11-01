{ pkgs, control }:

# Import from the actual flake Control closure, not a checkout/PYTHONPATH or a
# separately assembled test interpreter. No daemon, model, database or VM runs.
pkgs.runCommand "astrumweaver-audio-control-package" { } ''
  ${control}/bin/python -I -c '
import astrumweaver.control.audio_gateway_config as audio_config
from astrumweaver.gateway.audio_voice_api import create_audio_voice_router
from astrumweaver.gateway.audio_speech_api import create_audio_speech_router
from astrumweaver.gateway.audio_transcriptions_api import create_audio_transcriptions_router
from astrumweaver.gateway.audio import SPEECH_OPERATION_SCHEMA, TRANSCRIPTION_OPERATION_SCHEMA

assert audio_config.create_audio_voice_router is create_audio_voice_router
assert audio_config.create_audio_speech_router is create_audio_speech_router
assert audio_config.create_audio_transcriptions_router is create_audio_transcriptions_router
assert all(callable(factory) for factory in (
    create_audio_voice_router, create_audio_speech_router,
    create_audio_transcriptions_router,
))
assert SPEECH_OPERATION_SCHEMA == "openai-audio-speech-v1"
assert TRANSCRIPTION_OPERATION_SCHEMA == "openai-audio-transcription-metadata-v1"
'
  touch "$out"
''
