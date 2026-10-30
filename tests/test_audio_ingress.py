"""Offline synthetic ingress regressions; no routes, services or real audio."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from itertools import permutations
import json
import struct

import pytest
from starlette.requests import Request

from astrumweaver.gateway.audio import (
    SPEECH_OPERATION_SCHEMA,
    TRANSCRIPTION_OPERATION_SCHEMA,
    AudioGatewayError,
    AudioGatewayProfile,
    AudioProfileCatalog,
    VoiceAlias,
    audio_semantic_revision,
    validate_speech_request,
    validate_transcription_metadata,
)
from astrumweaver.gateway.audio_ingress import (
    AudioIngressLimits,
    parse_speech_ingress,
    parse_transcription_ingress,
)
from astrumweaver.serving import (
    DeploymentIdentity,
    LogicalServingProfile,
    ServingContract,
    resolve_profile,
)


def profile(stt=False, *, name=None, **tighten):
    kind = "model-stt" if stt else "voicevox"
    voices = () if stt else (VoiceAlias(
        "synthetic-voice", "synthetic-speaker", "0", "ja",
        "synthetic-license", "Synthetic attribution only",
    ),)
    deployment = DeploymentIdentity(
        provider_id="synthetic-provider" if stt else "voicevox",
        runtime_artifact_sha256="sha256:" + "1" * 64,
        adapter_artifact_sha256="sha256:" + "2" * 64,
        model_artifact_sha256="sha256:" + ("3" if stt else "4") * 64,
        execution_config_sha256="sha256:" + "5" * 64,
        quantization="synthetic-only",
    )
    operation = TRANSCRIPTION_OPERATION_SCHEMA if stt else SPEECH_OPERATION_SCHEMA
    capability = "audio.transcribe" if stt else "audio.synthesize"
    features = frozenset({"ja", "json" if stt else "wav"})
    contract = ServingContract(
        deployment_revision=deployment.revision, capability=capability,
        operation_schema=operation, validation_evidence_sha256="sha256:" + "6" * 64,
        semantic_revision=audio_semantic_revision(kind, voices), features=features,
        limits={"file_bytes": 256, "metadata_bytes": 128, "request_bytes": 2048}
        if stt else {"input_bytes": 24, "request_bytes": 2048},
    )
    logical = LogicalServingProfile(
        profile_id=name or ("synthetic-stt" if stt else "synthetic-tts"),
        deployment_revision=deployment.revision,
        serving_contract_revision=contract.revision, capability=capability,
        operation_schema=operation, required_features=features, limits=tighten,
    )
    return AudioGatewayProfile(resolve_profile(logical, contract, deployment), kind, voices)


@pytest.fixture
def catalog():
    return AudioProfileCatalog((profile(), profile(True)))


@pytest.fixture
def limits():
    return AudioIngressLimits(
        request_bytes=2048, input_bytes=24, file_bytes=256, metadata_bytes=128,
        part_header_bytes=512, total_header_bytes=2048, receive_events=4096,
        idle_timeout_seconds=0.2, total_timeout_seconds=1,
    )


def speech(**changes):
    return {"model": "synthetic-tts", "input": " 日本語 ", "voice": "synthetic-voice",
            "response_format": "wav", **changes}


def json_body(**changes):
    return json.dumps(speech(**changes), ensure_ascii=False).encode("utf-8")


def wav(samples=b"\x00\x00"):
    # Synthetic two-byte pattern, not captured or generated speech.
    return (b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", len(samples)) + samples)


def part(name, value, *, filename=None, mime=None, extra=b""):
    header = b'Content-Disposition: form-data; name="' + name.encode() + b'"'
    if filename is not None:
        header += b'; filename="' + filename.encode() + b'"'
    if mime is not None:
        header += b"\r\nContent-Type: " + mime.encode()
    return header + extra + b"\r\n\r\n" + value


def parts(*, audio=None, filename="synthetic.wav", mime="audio/wav"):
    return [
        part("model", b"synthetic-stt"), part("language", b"ja"),
        part("response_format", b"json"),
        part("file", wav() if audio is None else audio, filename=filename, mime=mime),
    ]


def multipart(values=None, boundary=b"synthetic_boundary"):
    values = parts() if values is None else values
    return (b"--" + boundary + b"\r\n"
            + (b"\r\n--" + boundary + b"\r\n").join(values)
            + b"\r\n--" + boundary + b"--\r\n")


JSON_TYPE = b"application/json"
MULTIPART_TYPE = b"multipart/form-data; boundary=synthetic_boundary"


class StubRequest:
    def __init__(self, body=b"", *, content_type=JSON_TYPE, headers=(), chunks=None, events=None):
        self.scope = {"type": "http", "headers": [(b"content-type", content_type), *headers]}
        if events is None:
            chunks = [body] if chunks is None else chunks
            events = [dict(type="http.request", body=chunk, more_body=i < len(chunks) - 1)
                      for i, chunk in enumerate(chunks)]
        self.events = iter(events)
        self.receives = 0

    async def receive(self):
        self.receives += 1
        return next(self.events)


async def transcribe(body, catalog, limits, **kwargs):
    return await parse_transcription_ingress(
        StubRequest(body, content_type=MULTIPART_TYPE, **kwargs), catalog, limits=limits,
    )


async def test_speech_matches_shared_contract_and_preserves_unicode(catalog, limits):
    raw = json_body()
    request = StubRequest(raw, headers=[(b"content-length", str(len(raw)).encode())])
    result = await parse_speech_ingress(request, catalog, limits=limits)
    assert result == validate_speech_request(speech(), catalog, request_size_bytes=len(raw))
    assert result.input == " 日本語 "
    assert result.input_size_bytes == 11


async def test_real_asgi_request_with_bytewise_chunking_and_no_length(catalog, limits):
    raw = json_body(input="あ😀")
    stub = StubRequest(chunks=[bytes([b]) for b in raw], headers=[(b"transfer-encoding", b"chunked")])
    request = Request(stub.scope, receive=stub.receive)
    assert (await parse_speech_ingress(request, catalog, limits=limits)).input == "あ😀"
    assert stub.receives == len(raw)


@pytest.mark.parametrize("content_type", [
    b"application/json; charset=utf-8", b'application/json; charset="utf-8"',
    b"APPLICATION/JSON; CHARSET=UTF-8",
])
async def test_utf8_json_media_parameters(catalog, limits, content_type):
    await parse_speech_ingress(StubRequest(json_body(), content_type=content_type),
                               catalog, limits=limits)


@pytest.mark.parametrize("raw", [
    b"", b"{}", b"[]", b"null", b"true", b"42", b"{", b"{}{}", b"{} trailing",
    b'\xef\xbb\xbf{}', b'{"input":"\xff"}', b'{"input":"\xc0\xaf"}',
    b'{"input":"unterminated}', b'{"input":"line\nfeed"}',
    b'{"input":"\\ud800"}', b'{"\\udfff":"value"}',
    b'{"model":"a","model":"b"}', b'{"model":"a","mo\\u0064el":"b"}',
    b'{"extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
    b'{"extra":{"nested":true}}', b'{"extra":NaN}', b'{"extra":Infinity}',
    b'{"extra":1e999}', b'{"extra":' + b"9" * 500 + b"}",
    b'{"a":"1","b":"2","c":"3","d":"4","e":"5"}',
])
async def test_bad_json_never_reaches_admission(catalog, limits, raw):
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(raw), catalog, limits=limits)


@pytest.mark.parametrize("duplicate", [b'"model":"synthetic-tts"',
                                       b'"mo\\u0064el":"synthetic-tts"',
                                       b'"input":"last value"'])
async def test_duplicate_json_in_otherwise_valid_request_is_not_last_wins(catalog, limits, duplicate):
    # Without a duplicate/member guard, json.loads would silently accept this.
    raw = json_body()[:-1] + b"," + duplicate + b"}"
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(raw), catalog, limits=limits)


async def test_pairs_hook_detects_decoded_duplicate_keys(catalog, limits):
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(StubRequest(b'{"model":"a","mo\\u0064el":"b"}'),
                                   catalog, limits=limits)
    assert error.value.code == "duplicate_fields"


async def test_deep_json_is_rejected_below_raw_cap(catalog, limits):
    raw = b'{"extra":' + b"[" * 500 + b"]" * 500 + b"}"
    assert len(raw) < limits.request_bytes
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(StubRequest(raw), catalog, limits=limits)
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize("changes", [
    {"backend_url": "https://private.invalid"}, {"stream": True},
    {"model": "https://private.invalid"}, {"model": "../../private"},
    {"model": "missing"}, {"model": "synthetic-stt"}, {"voice": "default"},
    {"voice": "../private"}, {"input": " "}, {"input": None},
    {"input": "あ" * 9}, {"response_format": "mp3"},
])
async def test_shared_speech_rejections_still_apply(catalog, limits, changes):
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(json_body(**changes)), catalog, limits=limits)


@pytest.mark.parametrize("missing", ["model", "input", "voice", "response_format"])
async def test_speech_no_omission_defaults(catalog, limits, missing):
    body = speech()
    del body[missing]
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(json.dumps(body).encode()), catalog, limits=limits)


async def test_json_delimiters_inside_strings_are_not_structure(catalog, limits):
    value = '[]{} ,,, " \\ 😀'
    result = await parse_speech_ingress(StubRequest(json_body(input=value)), catalog, limits=limits)
    assert result.input == value


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"9999999999999999999999")],
    [(b"content-length", b"2049")], [(b"content-length", b"0")],
    [(b"content-length", b"-1")], [(b"content-length", b"+1")],
    [(b"content-length", b" 123")], [(b"content-length", b"1, 1")],
    [(b"content-length", b"1"), (b"Content-Length", b"1")],
    [(b"content-type", JSON_TYPE)], [(b"content-encoding", b"gzip")],
    [(b"content-transfer-encoding", b"binary")],
    [(b"transfer-encoding", b"gzip, chunked")],
    [(b"transfer-encoding", b"chunked"), (b"content-length", b"1")],
    [(b"bad header", b"x")], [(b"x", b"a\r\nb")],
    [(b"x", b"a" * 2048)], [(b"x", b"\xff")],
])
async def test_header_rejections_happen_before_receive(catalog, limits, headers):
    request = StubRequest(json_body(), headers=headers)
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(request, catalog, limits=limits)
    assert request.receives == 0


@pytest.mark.parametrize("content_type", [
    b"text/json", b"application/json; charset=latin1", b"application/json; x=y",
    b"application/json; charset=utf-8; charset=utf-8", b"",
])
async def test_speech_mime_is_not_guessed(catalog, limits, content_type):
    request = StubRequest(json_body(), content_type=content_type)
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(request, catalog, limits=limits)
    assert request.receives == 0


@pytest.mark.parametrize("length", [1, 2048])
async def test_content_length_must_match_measured_bytes(catalog, limits, length):
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(
            StubRequest(json_body(), headers=[(b"content-length", str(length).encode())]),
            catalog, limits=limits,
        )
    assert error.value.code == "content_length_mismatch"


async def test_global_body_limit_precedes_decode_and_stops_consumption(catalog, limits):
    request = StubRequest(chunks=[b"not json", b"x" * 2048, b"MUST-NOT-READ"])
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(request, catalog, limits=limits)
    assert error.value.status_code == 413
    assert request.receives == 2


async def test_inclusive_raw_and_global_input_limits(catalog, limits):
    raw = json_body(input="あ" * 8)
    local = replace(limits, request_bytes=len(raw))
    assert (await parse_speech_ingress(StubRequest(raw), catalog, limits=local)).input_size_bytes == 24
    for changed in (replace(local, request_bytes=len(raw) - 1), replace(local, input_bytes=23)):
        with pytest.raises(AudioGatewayError):
            await parse_speech_ingress(StubRequest(raw), catalog, limits=changed)


async def test_selected_profile_limits_cannot_borrow_larger_sibling(catalog, limits):
    tight = profile(name="tight", input_bytes=3, request_bytes=150)
    combined = AudioProfileCatalog((*catalog.profiles, tight))
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(json_body(model="tight", input="ああ")),
                                   combined, limits=limits)
    raw = json_body(model="tight", input="あ") + b" " * 151
    with pytest.raises(AudioGatewayError):
        await parse_speech_ingress(StubRequest(raw), combined, limits=limits)


@pytest.mark.parametrize("events,code", [
    ([{"type": "http.disconnect"}], "request_disconnected"),
    ([{"type": "http.request", "body": b"{", "more_body": True},
      {"type": "http.disconnect"}], "request_disconnected"),
    ([{"type": "wrong"}], "invalid_body"),
    ([{"type": "http.request", "body": "not bytes"}], "invalid_body"),
    ([{"type": "http.request", "body": b"", "more_body": 1}], "invalid_body"),
    ([{"type": "http.request", "body": b""}], "empty_body"),
    ([None], "invalid_body"),
])
async def test_bad_receive_events_fail_closed(catalog, limits, events, code):
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(StubRequest(events=events), catalog, limits=limits)
    assert error.value.code == code


async def test_empty_chunk_flood_is_finite(catalog, limits):
    request = StubRequest(chunks=[b""] * 4 + [json_body()])
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(request, catalog, limits=replace(limits, receive_events=4))
    assert error.value.code == "too_many_receive_events"
    assert request.receives == 4


@pytest.mark.parametrize("total", [False, True])
async def test_idle_and_total_timeout(catalog, limits, total):
    class SlowRequest(StubRequest):
        async def receive(self):
            await asyncio.sleep(0.02 if total else 1)
            return {"type": "http.request", "body": b" ", "more_body": True}

    policy = replace(limits, idle_timeout_seconds=0.04 if total else 0.01,
                     total_timeout_seconds=0.05 if total else 0.1)
    with pytest.raises(AudioGatewayError) as error:
        await parse_speech_ingress(SlowRequest(), catalog, limits=policy)
    assert (error.value.code, error.value.status_code) == ("request_timeout", 408)


async def test_cancellation_is_not_swallowed(catalog, limits):
    class CancelledRequest(StubRequest):
        async def receive(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await parse_speech_ingress(CancelledRequest(), catalog, limits=limits)


@pytest.mark.parametrize("order", list(permutations(range(4))))
async def test_multipart_all_field_orders_and_exact_measurements(catalog, limits, order):
    values = parts()
    raw = multipart([values[i] for i in order])
    result = await transcribe(raw, catalog, limits)
    metadata = {"model": "synthetic-stt", "language": "ja", "response_format": "json"}
    size = sum(len(value.encode()) for value in metadata.values())
    assert result.metadata == validate_transcription_metadata(metadata, catalog,
                                                             metadata_size_bytes=size)
    assert result.wav_bytes == wav()
    assert result.request_size_bytes == len(raw)
    assert result.metadata_size_bytes == size
    assert not hasattr(result, "filename")
    with pytest.raises(FrozenInstanceError):
        result.wav_bytes = b"changed"


async def test_multipart_bytewise_chunking_quoted_boundary_and_metadata_mime(catalog, limits):
    values = parts()
    values[1] = part("language", b"ja", mime="text/plain; charset=utf-8")
    raw = multipart(values)
    stub = StubRequest(
        content_type=b'multipart/form-data; boundary="synthetic_boundary"',
        chunks=[bytes([b]) for b in raw],
    )
    result = await parse_transcription_ingress(Request(stub.scope, receive=stub.receive),
                                              catalog, limits=limits)
    assert result.wav_bytes == wav()


async def test_multipart_uses_same_prebuffer_transport_guards(catalog, limits):
    oversized = StubRequest(multipart(), content_type=MULTIPART_TYPE,
                            headers=[(b"content-length", b"2049")])
    with pytest.raises(AudioGatewayError) as error:
        await parse_transcription_ingress(oversized, catalog, limits=limits)
    assert error.value.status_code == 413
    assert oversized.receives == 0
    request = StubRequest(content_type=MULTIPART_TYPE,
                          chunks=[b"--synthetic_boundary\r\n", b"x" * 2048, b"UNREAD"])
    with pytest.raises(AudioGatewayError) as error:
        await parse_transcription_ingress(request, catalog, limits=limits)
    assert error.value.status_code == 413
    assert request.receives == 2
    for events in ([{"type": "http.disconnect"}], [{"type": "http.request", "body": b""}]):
        with pytest.raises(AudioGatewayError):
            await parse_transcription_ingress(
                StubRequest(content_type=MULTIPART_TYPE, events=events), catalog, limits=limits,
            )


@pytest.mark.parametrize("mime", ["text/plain; charset=latin1", "application/json", ""])
async def test_metadata_mime_does_not_change_decoding(catalog, limits, mime):
    values = parts()
    values[1] = part("language", b"ja", mime=mime)
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(values), catalog, limits)


@pytest.mark.parametrize("index", range(4))
async def test_missing_or_duplicate_multipart_fields(catalog, limits, index):
    values = parts()
    for changed in (values[:index] + values[index + 1:],
                    values[:index] + [values[index]] + values[index:]):
        with pytest.raises(AudioGatewayError):
            await transcribe(multipart(changed), catalog, limits)


@pytest.mark.parametrize("changed", [
    part("model", b"https://private.invalid"), part("model", b"../../private"),
    part("model", b"missing"), part("model", b"synthetic-tts"),
    part("model", b"synthetic-stt\xff"), part("model", b"synthetic-stt\x00"),
    part("model", b"synthetic-stt", filename="synthetic.wav"),
    part("backend_url", b"https://private.invalid"),
])
async def test_bad_multipart_metadata(catalog, limits, changed):
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart([changed, *parts()[1:]]), catalog, limits)


@pytest.mark.parametrize("field,value", [("language", b"en"), ("language", b""),
                                       ("response_format", b"text"), ("response_format", b"JSON")])
async def test_multipart_metadata_values_are_explicit(catalog, limits, field, value):
    values = parts()
    values[1 if field == "language" else 2] = part(field, value)
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(values), catalog, limits)


@pytest.mark.parametrize("content_type", [
    b"multipart/form-data", b"multipart/form-data; boundary=", b'multipart/form-data; boundary=""',
    b"multipart/form-data; boundary=" + b"a" * 71,
    b"multipart/form-data; boundary=../private", b"multipart/form-data; boundary=a b",
    b"multipart/form-data; boundary=synthetic_boundary; boundary=other",
    b"multipart/form-data; charset=utf-8; boundary=synthetic_boundary",
    b"multipart/mixed; boundary=synthetic_boundary", b"audio/wav",
])
async def test_multipart_mime_and_boundary_rejected_before_receive(catalog, limits, content_type):
    request = StubRequest(multipart(), content_type=content_type)
    with pytest.raises(AudioGatewayError):
        await parse_transcription_ingress(request, catalog, limits=limits)
    assert request.receives == 0


@pytest.mark.parametrize("transform", [
    lambda raw: b"preamble\r\n" + raw,
    lambda raw: raw + b"epilogue",
    lambda raw: raw[:-2],
    lambda raw: raw.replace(b"\r\n", b"\n"),
    lambda raw: raw.replace(b"--synthetic_boundary--", b"--wrong--"),
    lambda raw: raw.replace(b"\r\n--synthetic_boundary--", b"\r\n--synthetic_boundaryX--"),
])
async def test_malformed_multipart_framing(catalog, limits, transform):
    with pytest.raises(AudioGatewayError):
        await transcribe(transform(multipart()), catalog, limits)


@pytest.mark.parametrize("extra", [
    b"\r\nContent-Transfer-Encoding: base64", b"\r\nContent-Transfer-Encoding: binary",
    b"\r\nContent-Disposition: form-data; name=\"file\"",
    b"\r\nContent-Type: audio/wav", b"\r\nX-Unknown: value",
    b"\r\n folded: value", b"\r\nBad Header: value", b"\r\nX: \xff",
])
async def test_multipart_ambiguous_or_unknown_headers(catalog, limits, extra):
    values = parts()
    values[3] = part("file", wav(), filename="synthetic.wav", mime="audio/wav", extra=extra)
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(values), catalog, limits)


@pytest.mark.parametrize("filename", [
    "../private.wav", "/private.wav", "C:\\private.wav", "https://private.invalid/a.wav",
    "a%2fb.wav", "a\x00.wav", "日本語.wav", "a.WAV", "a.flac", "", None,
    'a.wav"; filename="other.wav', "a" * 65 + ".wav",
])
async def test_filename_is_not_a_path_or_url(catalog, limits, filename):
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(parts(filename=filename)), catalog, limits)


@pytest.mark.parametrize("mime", [
    "audio/flac", "application/octet-stream", "audio/x-wav", "audio/wav; codecs=1", None,
])
async def test_unsupported_file_mime_no_sniffing_or_transcode(catalog, limits, mime):
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(parts(mime=mime)), catalog, limits)


@pytest.mark.parametrize("audio", [
    b"", b"not audio", b"fLaC" + b"\x00" * 44, wav(b""), wav(b"x"),
    wav() + b"trailing", wav().replace(b"RIFF", b"RF64"),
    wav().replace(b"WAVE", b"wave"), wav().replace(b"data", b"JUNK"),
    wav().replace(b"fmt ", b"LIST"),
    wav()[:20] + struct.pack("<H", 3) + wav()[22:],
    wav()[:22] + struct.pack("<H", 0) + wav()[24:],
    wav()[:24] + struct.pack("<I", 192000) + wav()[28:],
    wav()[:28] + struct.pack("<I", 1) + wav()[32:],
    wav()[:32] + struct.pack("<H", 1) + wav()[34:],
    wav()[:34] + struct.pack("<H", 8) + wav()[36:],
    wav()[:40] + struct.pack("<I", 100) + wav()[44:],
    b"\r\n--synthetic_boundaryX" + wav(),
])
async def test_empty_spoofed_or_unsupported_wav(catalog, limits, audio):
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(parts(audio=audio)), catalog, limits)


async def test_global_and_profile_file_metadata_aggregate_bounds(catalog, limits):
    raw = multipart()
    metadata_size = len(b"synthetic-stt") + 2 + 4
    exact = replace(limits, request_bytes=len(raw), file_bytes=len(wav()),
                    metadata_bytes=metadata_size)
    assert (await transcribe(raw, catalog, exact)).wav_bytes == wav()
    for policy in (
        replace(exact, request_bytes=len(raw) - 1),
        replace(exact, file_bytes=len(wav()) - 1),
        replace(exact, metadata_bytes=metadata_size - 1),
    ):
        with pytest.raises(AudioGatewayError) as error:
            await transcribe(raw, catalog, policy)
        assert error.value.status_code == 413
    values = parts()
    values[0] = part("model", b"tight-stt")
    for tighten in ({"file_bytes": len(wav()) - 1},
                    {"metadata_bytes": len(b"tight-stt") + 5}):
        # Retain a larger sibling: global/max-catalog caps alone must not pass.
        tight_catalog = AudioProfileCatalog((*catalog.profiles, profile(True, name="tight-stt", **tighten)))
        with pytest.raises(AudioGatewayError) as error:
            await transcribe(multipart(values), tight_catalog, limits)
        assert error.value.status_code == 413
    tight_request = len(multipart(values)) - 1
    assert tight_request > 256 + 128  # valid profile envelope, not a config error
    tight_catalog = AudioProfileCatalog((*catalog.profiles, profile(
        True, name="tight-stt", request_bytes=tight_request,
    )))
    with pytest.raises(AudioGatewayError) as error:
        await transcribe(multipart(values), tight_catalog, limits)
    assert error.value.status_code == 413


async def test_header_caps_parts_and_total_are_bounded(catalog, limits):
    raw = multipart()
    for policy in (replace(limits, part_header_bytes=16), replace(limits, total_header_bytes=110)):
        with pytest.raises(AudioGatewayError):
            await transcribe(raw, catalog, policy)
    # Headers fit individually but exceed the aggregate multipart-header budget.
    assert all(len(value.split(b"\r\n\r\n")[0]) < 110 for value in parts())
    with pytest.raises(AudioGatewayError):
        await transcribe(multipart(parts() + [part("file", wav(), filename="other.wav", mime="audio/wav")]),
                         catalog, limits)


async def test_exact_part_and_aggregate_header_caps(catalog, limits):
    values = parts()
    sizes = [len(value.split(b"\r\n\r\n")[0]) for value in values]
    exact = replace(limits, part_header_bytes=max(sizes),
                    total_header_bytes=sum(size + 4 for size in sizes))
    await transcribe(multipart(values), catalog, exact)
    for changed in (replace(exact, part_header_bytes=max(sizes) - 1),
                    replace(exact, total_header_bytes=exact.total_header_bytes - 1)):
        with pytest.raises(AudioGatewayError):
            await transcribe(multipart(values), catalog, changed)


async def test_errors_do_not_echo_private_keys_values_or_filenames(catalog, limits):
    for request, parser in (
        (StubRequest(json_body(**{"PRIVATE-SENTINEL": "https://private.invalid"})), parse_speech_ingress),
        (StubRequest(multipart(parts(filename="../PRIVATE-SENTINEL.wav")), content_type=MULTIPART_TYPE),
         parse_transcription_ingress),
    ):
        with pytest.raises(AudioGatewayError) as error:
            await parser(request, catalog, limits=limits)
        rendered = json.dumps(error.value.to_dict())
        assert "PRIVATE-SENTINEL" not in rendered
        assert "private.invalid" not in rendered
        assert error.value.__cause__ is None


@pytest.mark.parametrize("changes", [
    {"request_bytes": True}, {"request_bytes": 0}, {"request_bytes": 16 * 1024 * 1024 + 1},
    {"file_bytes": -1}, {"metadata_bytes": 1.5}, {"receive_events": 0},
    {"part_header_bytes": 8193}, {"total_header_bytes": 32769},
    {"idle_timeout_seconds": float("nan")}, {"idle_timeout_seconds": float("inf")},
    {"idle_timeout_seconds": True}, {"total_timeout_seconds": 0},
    {"total_timeout_seconds": 61}, {"idle_timeout_seconds": 10 ** 1000},
    {"idle_timeout_seconds": 2, "total_timeout_seconds": 1},
])
def test_operator_limits_are_finite_positive_and_explicit(limits, changes):
    with pytest.raises(AudioGatewayError) as error:
        replace(limits, **changes)
    assert error.value.status_code == 500


async def test_missing_operation_fails_before_read(catalog, limits):
    for parser, selected in ((parse_speech_ingress, profile(True)),
                             (parse_transcription_ingress, profile())):
        request = StubRequest(json_body())
        with pytest.raises(AudioGatewayError) as error:
            await parser(request, AudioProfileCatalog((selected,)), limits=limits)
        assert error.value.status_code == 503
        assert request.receives == 0
