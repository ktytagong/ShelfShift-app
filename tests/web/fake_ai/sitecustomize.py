"""Explicit offline reading scripts, injected only into the browser-test API child.

The application imports no fake. This module never constructs a provider client.
"""
import os

if os.environ.get('SS_FAKE_AI') == '1':
    import asyncio
    import hashlib
    import json
    from types import SimpleNamespace

    from agent import readings_job
    from agent.reading import Evidence, Reading

    _PNG = bytes.fromhex(
        '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d'
        '0000000049454e44ae426082')
    PHOTO_ONLY = _PNG + b'Plan11-photo-only'
    REPORT_PHOTO = _PNG + b'Plan13-capacity-photo'
    REPORT_PDF = b'%PDF-1.4\n% Plan13 explicit quote fixture\n%%EOF'
    LONG_EXCERPT = 'long excerpt ' * 15
    def attachment_report(context, quote=False):
        dates = sorted(context['calendar'], key=context['calendar'].get)
        if quote:
            values = dict(kind='TRANSPORT', run_code=context['run_codes'][0], what='QUOTE', amount=12.5,
                pricing_basis='PER_TRIP', includes_handling='YES', applies_on=dates[0], valid_until=dates[-1])
        else:
            values = dict(kind='CAPACITY', zone='chilled', free_totes=6, valid_from=dates[0], valid_to=dates[-1])
        return Reading(**values, evidence=[Evidence(field=field, source='file' if quote else 'photo',
            excerpt='Quote terms' if quote else 'Capacity notice', confidence=.9) for field in values])
    def long_report(context):
        result = chiller(context)
        return result.model_copy(update={'evidence': [e.model_copy(update={'excerpt': LONG_EXCERPT}) for e in result.evidence]})
    def chiller(context):
        dates = sorted(context['calendar'], key=context['calendar'].get)
        excerpt = 'half the chilled stock until tomorrow'
        return Reading(kind='CAPACITY', zone='chilled', capacity_share=0.5,
            valid_from=dates[0], valid_to=dates[min(1, len(dates)-1)],
            evidence=[Evidence(field=field, source='message', excerpt=excerpt, confidence=.9)
                for field in ('kind', 'zone', 'capacity_share', 'valid_from', 'valid_to')])
    def summary_chiller(context):
        dates = sorted(context['calendar'], key=context['calendar'].get)
        return Reading(kind='CAPACITY', zone='chilled', free_totes=0,
            valid_from=dates[0], valid_to=dates[min(1, len(dates)-1)],
            evidence=[Evidence(field=field, source='message', excerpt='no chilled capacity until tomorrow', confidence=.9)
                for field in ('kind', 'zone', 'free_totes', 'valid_from', 'valid_to')])
    def unknown_lot(_context):
        return Reading(kind='STOCK_COUNT', lot_id='NOT-A-LOT', shelf_units=1, backroom_units=0,
            evidence=[Evidence(field=field, source='message', excerpt='Plan13 unknown lot', confidence=.9)
                for field in ('kind', 'lot_id', 'shelf_units', 'backroom_units')])
    # Keys match a text substring or an attachment SHA256. Values are (action, delay).
    # A callable action receives the public reading context and returns a Reading.
    # Later web plans can add explicit report scripts without changing product code.
    SCRIPTS = {
        ('text', 'Plan16 chiller:'): (summary_chiller, 0.1),
        ('text', 'Plan11 delayed reply'): ('reply-zero', 5.0),
        ('text', 'Plan13 long report'): (long_report, .1),
        ('sha256', hashlib.sha256(REPORT_PHOTO).hexdigest()): (attachment_report, .1),
        ('sha256', hashlib.sha256(REPORT_PDF).hexdigest()): (lambda c: attachment_report(c, quote=True), .1),
        ('text', 'Plan13 chiller:'): (chiller, 0.8),
        ('text', 'Staff party Friday 6 pm, everyone welcome!'): (lambda _: Reading(kind='NOT_RELEVANT'), .1),
        ('text', 'Plan13 unknown lot'): (unknown_lot, .1),
        ('sha256', hashlib.sha256(PHOTO_ONLY).hexdigest()): ('photo-empty', 2.0),
        ('text', 'None on the shelf, none in the backroom either.'): ('reply-zero', 0.05),
        ('text', 'Plan11 unreadable answer'): ('failed', 0.01),
    }

    class ScriptedModels:
        async def count_tokens(self, **_kwargs):
            return SimpleNamespace(total_tokens=100)

        async def generate_content(self, *, contents, **_kwargs):
            parts = contents[0].parts
            text = parts[0].text
            context = json.loads(text.split('\nREPORT_DATA ', 1)[0])
            binary = next((p.inline_data.data for p in parts if p.inline_data is not None), None)
            digest = hashlib.sha256(binary).hexdigest() if binary else None
            action, delay = next((value for (kind, key), value in SCRIPTS.items()
                if (kind == 'sha256' and key == digest) or (kind == 'text' and key in text)), ('failed', 0))
            await asyncio.sleep(delay)
            parsed = None
            if callable(action):
                parsed = action(context)
            elif action == 'photo-empty':
                parsed = Reading(kind='STOCK_COUNT', photo_observation='visible slot empty')
            elif action == 'reply-zero':
                excerpt = 'None on the shelf, none in the backroom either.'
                parsed = Reading(kind='STOCK_COUNT', lot_id=context['lot_ids'][0], shelf_units=0,
                    backroom_units=0, evidence=[Evidence(field=field, source='message',
                        excerpt=excerpt, confidence=.9) for field in
                        ('kind', 'lot_id', 'shelf_units', 'backroom_units')])
            return SimpleNamespace(parsed=parsed, candidates=[SimpleNamespace(finish_reason='STOP')],
                usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=30,
                                                thoughts_token_count=5, total_token_count=135))

    async def close():
        pass

    readings_job.genai_client = lambda: SimpleNamespace(aio=SimpleNamespace(models=ScriptedModels(), aclose=close))

    # The real Runner, preview tool, number checker and usage persistence remain
    # in charge. This model only emits tool calls and cites the result it receives.
    from agent import model
    from google.adk.models import BaseLlm, LlmResponse
    from google.genai import types

    class ImpactModel(BaseLlm):
        model: str = 'offline-impact-model'

        async def generate_content_async(self, llm_request, stream=False):
            replies = [p.function_response for c in llm_request.contents for p in (c.parts or [])
                if p.function_response is not None and p.function_response.name == 'get_impact_preview']
            await asyncio.sleep(1.0)
            if not replies:
                parts = [types.Part(function_call=types.FunctionCall(name='get_impact_preview', args={}))]
            else:
                payload = replies[-1].response
                # An explicit delayed terminal for observer/epoch browser tests.
                if payload['fields'].get('free_totes') == 2:
                    await asyncio.sleep(3.0)
                if payload['fields'].get('free_totes') == 1:
                    raise RuntimeError('Explicit offline summary failure script')
                selected = next((i for i, row in enumerate(payload['cases']) if row.get('benefit_if') is not None), None)
                assert selected is not None, 'The summary tracer needs an actual previewed case benefit'
                parts = [types.Part(text=f"If confirmed, the previewed case would have net benefit [[{payload['result_id']}:cases.{selected}.benefits.if_confirmed.net]].")]
            yield LlmResponse(content=types.Content(role='model', parts=parts),
                usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=20,
                    candidates_token_count=10, thoughts_token_count=1, total_token_count=31))

    model.llm = lambda: ImpactModel()
