import json
import pytest
from agent.captions import CaptionError,caption_instruction,parse_caption,checked_caption


def response(caption='A woman wearing a black shirt holds a rose outdoors at night.', **extra):
    return json.dumps({'caption':caption,'person_visible':True,'face_visible':True,**extra})


def test_clean_prefix_quotes_and_trigger_once():
    result=parse_caption(response('Caption: "Vivi_model, Vivi_model, A woman holds a rose outdoors at night."'),'Vivi_model')
    assert result['caption']=='Vivi_model, A woman holds a rose outdoors at night.'
    assert result['quality']['person_visible'] is True
    assert result['quality']['caption_review_required'] is True


def test_fenced_json_and_uncertain_visibility():
    result=parse_caption('```json\n'+response(person_visible=None,face_visible=None)+'\n```','MayMay_model')
    assert result['quality']['face_visible'] is None


@pytest.mark.parametrize('raw,truncated',[
    ('Caption: A woman holds a rose.',False),
    ('[]',False),
    (response(''),False),
    (response('I cannot describe this image or assist with this request.'),False),
    (response(face_visible='yes'),False),
    (response(person_visible=False),False),
    (response('A woman wearing a dark shirt stands beside a road...'),False),
    (response('A woman called Vivi_model stands beside a road.'),False),
    (response(),True),
])
def test_invalid_outputs_are_rejected(raw,truncated):
    with pytest.raises(CaptionError):parse_caption(raw,'Vivi_model',truncated)


def test_retry_is_bounded_and_failed_output_has_no_replacement_caption():
    calls=[]
    def invalid(attempt):
        calls.append(attempt)
        return 'not json',False
    result=checked_caption(invalid,'Vivi_model')
    assert calls==[0,1]
    assert 'caption' not in result
    assert result['quality']['caption_status']=='failed'
    assert result['quality']['caption_error']


def test_retry_can_recover():
    result=checked_caption(lambda n: ('invalid',False) if n==0 else (response(),False),'Vivi_model')
    assert result['quality']['caption_status']=='generated'
    assert result['quality']['caption_attempts']==2


def test_video_prompt_preserves_rules_and_demands_structure():
    prompt=caption_instruction('Describe visible accessories.',video=True,retry=True)
    assert 'chronological' in prompt and 'JSON' in prompt
    assert 'Describe visible accessories.' in prompt and 'previous response' in prompt
