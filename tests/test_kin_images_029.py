from bs4 import BeautifulSoup

from takuro_collector.sites.kinoshita import KinoshitaAdapter


def test_kin_current_property_gallery_excludes_recommendations_and_site_artwork():
    html = r'''
    <html><body>
      <h1>フィオーネ柴又 203号室</h1>
      <dl>
        <dt>所在地</dt><dd>東京都葛飾区柴又1-2-3</dd>
        <dt>家賃</dt><dd>80,000円</dd>
        <dt>管理費</dt><dd>5,000円</dd>
      </dl>

      <ul id="list_images">
        <li><a href="/upload/current/exterior.jpg"><img src="/upload/current/exterior_thumb.jpg" alt="外観"></a></li>
        <li><a href="/upload/current/madori.png"><img src="/upload/current/madori_thumb.png" alt="間取り"></a></li>
        <li><a href="/upload/current/room01.jpg"><img src="/upload/current/room01_thumb.jpg" alt="洋室"></a></li>
      </ul>

      <section class="recommend-property">
        <h2>フィオーネ柴又をご覧の皆様に、こちらの賃貸物件もオススメです</h2>
        <a href="/details/otherdetails.html"><img src="/upload/other/other-building.jpg" alt="別物件外観"></a>
      </section>
      <div class="other-room"><img src="/upload/other/other-room.jpg" alt="別のお部屋"></div>
      <div class="banner"><img src="/common/campaign-banner.jpg" alt="キャンペーン"></div>
      <footer><img src="/common/logo.png" alt="木下の賃貸"></footer>
    </body></html>
    '''
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/14261_2details.html')
    urls = [x['url'] for x in p.photo_sources]
    assert urls == [
        'https://kinoshita-chintai.com/upload/current/exterior.jpg',
        'https://kinoshita-chintai.com/upload/current/madori.png',
        'https://kinoshita-chintai.com/upload/current/room01.jpg',
    ]
    assert not any('/other/' in u for u in urls)
    assert not any('/common/' in u for u in urls)


def test_kin_gallery_prefers_fullsize_link_over_thumbnail_and_dedupes_resize_variants():
    html = r'''
    <html><body>
      <ul id="list_images">
        <li><a href="/images/room01.jpg?w=1600&v=1"><img src="/images/thumb/room01.jpg?w=300&v=2" alt="洋室"></a></li>
        <li><a href="/images/room01.jpg?w=800&v=3"><img src="/images/thumb/room01.jpg?w=200&v=4" alt="洋室"></a></li>
      </ul>
    </body></html>
    '''
    soup = BeautifulSoup(html, 'html.parser')
    got = KinoshitaAdapter._property_photo_sources(soup, 'https://kinoshita-chintai.com/details/x.html')
    assert len(got) == 1
    assert got[0]['url'].startswith('https://kinoshita-chintai.com/images/room01.jpg')


def test_kin_fallback_removes_recommendation_blocks_when_gallery_id_missing():
    html = r'''
    <html><body>
      <main>
        <div class="photos"><a href="/current/a.jpg"><img src="/current/a-thumb.jpg" alt="室内"></a></div>
        <section><h2>こちらの賃貸物件もオススメです</h2><img src="/other/recommend.jpg" alt="外観"></section>
      </main>
      <footer><img src="/common/logo.png" alt="logo"></footer>
    </body></html>
    '''
    soup = BeautifulSoup(html, 'html.parser')
    got = KinoshitaAdapter._property_photo_sources(soup, 'https://kinoshita-chintai.com/details/x.html')
    urls = [x['url'] for x in got]
    assert 'https://kinoshita-chintai.com/current/a.jpg' in urls
    assert not any('recommend.jpg' in u for u in urls)
    assert not any('logo.png' in u for u in urls)
