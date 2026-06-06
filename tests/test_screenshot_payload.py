from ui.workers.screenshot_payload import make_pending_screen_attachment


def test_make_pending_screen_attachment_structure():
    attachment = make_pending_screen_attachment(
        image_bytes=b"png",
        image_metadata={
            "original_resolution": {"width": 400, "height": 300},
            "sent_resolution": {"width": 300, "height": 225},
            "downscaled": True,
            "format": "png",
        },
        captured_at="2026-06-06T00:00:00+00:00",
        screen_name="primary",
        screen_index=0,
        logical_rect={"x": 1, "y": 2, "width": 400, "height": 300},
        physical_rect={"x": 2, "y": 4, "width": 800, "height": 600},
        device_pixel_ratio=2.0,
    )

    assert attachment["kind"] == "screen_capture"
    assert attachment["target"] == "selected_region"
    assert attachment["mode"] == "region"
    assert attachment["source"] == "manual_region_selection"
    assert attachment["created_at"] == "2026-06-06T00:00:00+00:00"
    assert attachment["image_bytes"] == b"png"
    assert attachment["mime_type"] == "image/png"
    assert attachment["width"] == 300
    assert attachment["height"] == 225
    assert attachment["original_resolution"] == {"width": 400, "height": 300}
    assert attachment["sent_resolution"] == {"width": 300, "height": 225}
    assert attachment["downscaled"] is True
    assert attachment["format"] == "png"
    assert attachment["quality"] is None
    assert attachment["region"]["screen_name"] == "primary"
    assert attachment["region"]["screen_index"] == 0
    assert attachment["region"]["device_pixel_ratio"] == 2.0
