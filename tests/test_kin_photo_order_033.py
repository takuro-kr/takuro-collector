from bs4 import BeautifulSoup

from takuro_collector.sites.kinoshita import KinoshitaAdapter


def image(name: str, parent_class: str = 'swiper-slide') -> str:
    return f'<div class="{parent_class}"><a href="/img.php?img={name}"><img src="/img.php?img={name}&type=T"></a></div>'


def test_kin_floorplan_is_included_and_photos_follow_staff_order():
    html = ''.join([
        image('813013_011255_0004_06.jpg', 'main_image_madori_eria'),
        '<div id="list_images">',
        image('813013_011255_0004_30.jpg'),       # bath
        image('813013_011255_0004_28.jpg'),       # toilet
        image('813013_011255_0004_25.jpg'),       # kitchen
        image('813013_011255_0004_23.jpg'),       # whole room
        image('813013_011255_06.jpg'),            # primary exterior
        image('813013_011255_07.jpg'),            # remaining exterior
        image('813013_011255_0004_33.jpg'),       # other interior
        '</div>',
    ])
    got = KinoshitaAdapter._property_photo_sources(BeautifulSoup(html, 'html.parser'), 'https://kinoshita-chintai.com/x')
    assert [x['kind'] for x in got] == [
        'exterior', 'floorplan', 'interior_other', 'interior_other',
        'interior_other', 'interior_other', 'interior_other', 'exterior',
    ]
    assert all('type=T' not in x['url'] for x in got)
