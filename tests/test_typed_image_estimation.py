"""Typed pixels must not become text tokens or change the request body."""

import copy

import pytest

from amplifier_module_context_simple._text_estimate import estimate_messages


def test_typed_image_size_is_unknown_and_transport_size_does_not_drive_text_budget():
    small = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe it"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "x"},
            },
        ],
    }
    large = copy.deepcopy(small)
    large["content"][1]["source"]["data"] = "x" * 8_500_000
    before = copy.deepcopy(large)
    assert estimate_messages([large]) == estimate_messages([small])
    assert estimate_messages([large]).has_unmeasured_images
    assert large == before


@pytest.mark.parametrize(
    "content",
    [
        "data:image/png;base64," + "x" * 10000,
        [
            {
                "type": "text",
                "text": '{"type":"image","source":{"data":"' + "x" * 10000 + '"}}',
            }
        ],
        [
            {
                "type": "tool_call",
                "name": "test",
                "arguments": {"type": "image", "data": "x" * 10000},
            }
        ],
    ],
)
def test_quoted_images_and_tool_arguments_still_count_as_text(content):
    message = {"role": "user", "content": content}
    estimate = estimate_messages([message])
    assert estimate.tokens == len(str(message)) // 4
    assert not estimate.has_unmeasured_images


@pytest.mark.parametrize(
    "image",
    [
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.test/" + "x" * 10000},
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "x" * 10000},
        },
        {"type": "input_image", "image_url": "data:image/png;base64," + "x" * 10000},
    ],
)
def test_multiple_tool_screenshots_have_unknown_pixel_cost(image):
    message = {
        "role": "tool",
        "content": [{"type": "tool_result", "content": [image, image]}],
    }
    before = copy.deepcopy(message)
    estimate = estimate_messages([message])
    assert estimate.tokens < 100
    assert estimate.has_unmeasured_images
    assert message == before


@pytest.mark.asyncio
async def test_protected_image_survives_hard_fit_while_real_text_overflow_still_fails():
    from amplifier_core import ContextLengthError

    from amplifier_module_context_simple import SimpleContextManager

    context = SimpleContextManager(max_tokens=1000, compaction_notice_enabled=False)
    image = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "x" * 8_500_000,
        },
    }
    await context.add_message(
        {"role": "user", "content": [{"type": "text", "text": "CURRENT"}, image]}
    )
    view = await context.get_messages_for_request_retaining(
        retain_contents=[], hard_fit=True, token_budget=1000
    )
    assert view[0]["content"][1] == image
    assert context._last_token_meter_stats["estimate_scope"] == "text_only"
    assert context._last_token_meter_stats["measured_tokens"] is None
    await context.add_message({"role": "user", "content": "LARGE-TEXT" * 1000})
    with pytest.raises(ContextLengthError):
        await context.get_messages_for_request_retaining(
            retain_contents=[], hard_fit=True, token_budget=1000
        )
    assert (await context.get_messages())[0]["content"][1] == image


@pytest.mark.parametrize("content", ["Images from tool", [{"type": "text", "text": "Images"}]])
def test_canonical_media_refs_have_unknown_cost_without_changing_text_estimate(
    content, media_metadata
):
    message = {"role": "tool", "content": content, "metadata": media_metadata}
    before = copy.deepcopy(message)
    estimate = estimate_messages([message])
    assert estimate.has_unmeasured_images
    assert estimate.tokens == len(str(message)) // 4
    assert message == before


@pytest.mark.parametrize(
    "message",
    [
        {"content": '{"experimental.media.v1":{"refs":[{"sha256":"quoted"}]}}'},
        {"content": [{"type": "text", "text": 'experimental.media.v1 refs ["quoted"]'}]},
        {"tool_calls": [{"arguments": {"experimental.media.v1": {"refs": ["argument"]}}}]},
        {"metadata": {"nested": {"experimental.media.v1": {"refs": ["nested"]}}}},
        {"metadata": {"experimental.media.v2": {"refs": ["future"]}}},
        {"metadata": {"experimental.media.v1": {"refs": []}}},
        {"metadata": {"experimental.media.v1": None}},
        {"metadata": {"experimental.media.v1": '{"refs":["quoted"]}'}},
        {"metadata": {"experimental.media.v1": {"refs": "not-a-list"}}},
        {"metadata": None},
    ],
)
def test_only_direct_canonical_ref_lists_mark_unknown_image_cost(message):
    message = {"role": "user", **message}
    estimate = estimate_messages([message])
    assert not estimate.has_unmeasured_images
    assert estimate.tokens == len(str(message)) // 4


@pytest.mark.asyncio
@pytest.mark.parametrize("token_meter", ["estimate", "actual"])
async def test_refs_are_text_only_hints_and_cannot_certify_estimate_hard_fit(
    token_meter, media_metadata
):
    from amplifier_core import ContextLengthError

    from amplifier_module_context_simple import SimpleContextManager

    context = SimpleContextManager(
        max_tokens=10_000, token_meter=token_meter, compaction_notice_enabled=False
    )
    await context.add_message(
        {"role": "user", "content": "Describe the images", "metadata": media_metadata}
    )
    # A previous response count is not a measurement of this request's images.
    context.claim_foreground_usage()(input_tokens=1)
    view = await context.get_messages_for_request()
    assert view[0]["metadata"] == media_metadata
    assert context._last_token_meter_stats["estimate_scope"] == "text_only"
    before = copy.deepcopy(await context.get_messages())
    stats = context._last_token_meter_stats
    with pytest.raises(ContextLengthError, match="media references.*measured"):
        await context.get_messages_for_request_retaining(
            retain_contents=[], hard_fit=True, token_budget=10_000
        )
    assert await context.get_messages() == before
    assert context._last_token_meter_stats is stats
    assert not context._removed_seqs
    assert not context._truncated_seqs
    assert context._request_hard_fit is False


@pytest.mark.asyncio
async def test_host_unavailable_text_projection_needs_no_media_cost_certification():
    from amplifier_module_context_simple import SimpleContextManager

    context = SimpleContextManager(max_tokens=1_000, compaction_notice_enabled=False)
    text = "[Image unavailable: selected route has no vision; no pixels supplied]"
    await context.add_message({"role": "user", "content": text})
    view = await context.get_messages_for_request_retaining(
        retain_contents=[], hard_fit=True, token_budget=1_000
    )
    assert view[0]["content"] == text
    assert context._last_token_meter_stats["estimate_scope"] == "all_text"


@pytest.mark.asyncio
async def test_unprotected_surviving_refs_also_block_final_estimate_hard_fit(media_metadata):
    from amplifier_core import ContextLengthError

    from amplifier_module_context_simple import SimpleContextManager

    context = SimpleContextManager(max_tokens=10_000, compaction_notice_enabled=False)
    await context.add_message({"role": "user", "content": "first human"})
    await context.add_message(
        {"role": "tool", "content": "Images", "metadata": media_metadata}
    )
    await context.add_message({"role": "user", "content": "last human"})
    before = copy.deepcopy(await context.get_messages())
    with pytest.raises(ContextLengthError, match="media references.*measured"):
        await context.get_messages_for_request_retaining(
            retain_contents=[], hard_fit=True, token_budget=10_000
        )
    assert await context.get_messages() == before
    assert context._last_token_meter_stats is None
