"""Caption output contract; deliberately independent of GPU libraries."""
import json
import re


class CaptionError(ValueError):
    pass


def caption_instruction(rules='', video=False, retry=False):
    text = (
        'Return ONLY a JSON object with keys caption (English string), '
        'person_visible (boolean or null), face_visible (boolean or null). '
        'Write one concise factual LoRA training caption, 30-90 words. '
        'Describe only visible framing, pose, expression, clothing, lighting and background. '
        'Do not infer identity, age, personality, hidden body parts or unseen details. '
        'Do not include a trigger word, headings, Caption:, explanations or markdown. '
        'Use null when visibility is uncertain. '
    )
    if video:
        text += ('These are sampled frames from one video, in chronological order. '
                 'Describe supported actions and changes, not imagined motion between frames. ')
    if rules:
        text += 'Dataset description preferences: ' + str(rules)[:6000] + '\n'
    text += 'Output format: {"caption":"...","person_visible":true,"face_visible":true}.'
    if retry:
        text += ' The previous response was invalid. Return a complete JSON object only.'
    return text


def parse_caption(raw, trigger, truncated=False):
    if truncated:
        raise CaptionError('Ответ оборван: превышен лимит длины')
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CaptionError('Модель не вернула корректный JSON') from exc
    if not isinstance(value, dict) or not isinstance(value.get('caption'), str):
        raise CaptionError('В ответе отсутствует текст caption')
    for key in ('person_visible', 'face_visible'):
        if key not in value or (value[key] is not None and type(value[key]) is not bool):
            raise CaptionError('Некорректный флаг видимости: ' + key)
    if value['person_visible'] is False and value['face_visible'] is True:
        raise CaptionError('Противоречивые флаги видимости')
    caption = re.sub(r'\s+', ' ', value['caption']).strip()
    # The trigger is application-owned; strip repeated model-generated prefixes.
    prefix = r'(?i)^(?:caption\s*:\s*|' + re.escape(trigger.strip()) + r'\s*[,;:]\s*)'
    if not trigger.strip():
        prefix = r'(?i)^caption\s*:\s*'
    for _ in range(12):
        cleaned = re.sub(prefix, '', caption.strip(' \"\'“”'))
        if cleaned == caption:
            break
        caption = cleaned
    caption = caption.strip(' \"\'“”')
    if re.search(r"\b(?:i (?:cannot|can't|am unable)|i(?:'m| am) sorry|as an ai|unable to (?:describe|assist)|cannot (?:describe|assist))\b", caption, re.I):
        raise CaptionError('Модель отказалась от описания; нужна ручная проверка')
    if not 5 <= len(caption.split()) <= 160 or not re.search('[a-zA-Z]', caption):
        raise CaptionError('Подпись пуста, слишком короткая или слишком длинная')
    if caption.endswith(('...', '…', ',', ':', ';')):
        raise CaptionError('Подпись выглядит незавершённой')
    if trigger.strip() and re.search(r'(?<!\w)' + re.escape(trigger.strip()) + r'(?!\w)', caption, re.I):
        raise CaptionError('Модель добавила trigger внутрь описания')
    return {
        'caption': (trigger.strip() + ', ' if trigger.strip() else '') + caption,
        'quality': {'person_visible': value['person_visible'], 'face_visible': value['face_visible'],
                    'caption_review_required': True, 'caption_error': '', 'caption_status': 'generated'},
    }


def checked_caption(generate, trigger):
    """One bounded retry. Invalid text must never replace an existing caption."""
    error = ''
    for attempt in range(2):
        raw, truncated = generate(attempt)
        try:
            result = parse_caption(raw, trigger, truncated)
            result['quality']['caption_attempts'] = attempt + 1
            return result
        except CaptionError as exc:
            error = str(exc)
    return {'quality': {'caption_review_required': True, 'caption_status': 'failed',
                        'caption_error': error, 'caption_attempts': 2}}
