from io import BytesIO

from PIL import Image

from takuro_collector.photos import _prepare_photo, _trim_white_margin


def test_paired_white_side_padding_is_removed():
    image = Image.new('RGB', (640, 640), 'white')
    for x in range(80, 560):
        for y in range(640):
            image.putpixel((x, y), (80, 120, 160))
    assert _trim_white_margin(image).size == (480, 640)


def test_photo_is_not_upscaled_and_gets_visible_center_watermark():
    image = Image.new('RGB', (320, 240), (60, 90, 120))
    source = BytesIO()
    image.save(source, format='JPEG')
    data, ext = _prepare_photo(source.getvalue(), 'jpg')
    result = Image.open(BytesIO(data)).convert('RGB')
    assert ext == 'jpg'
    assert result.size == image.size
    assert result.getpixel((160, 120)) != image.getpixel((160, 120))
