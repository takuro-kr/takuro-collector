from takuro_collector.sites.kinoshita import KinoshitaAdapter


def test_kin_rejects_seo_title_address_and_splits_deposit_key_money():
    html = '''
    <html>
      <head><title>神奈川県、逗子市の賃貸物件情報/仲介手数料無料(不要)|木下の賃貸</title></head>
      <body>
        <h1>アンプルール クラージュ 逗子A 101号室</h1>
        <div class="breadcrumb">神奈川県、逗子市の賃貸物件情報/仲介手数料無料(不要)|木下の賃貸</div>
        <div class="property-location"><span>所在地</span><span>神奈川県逗子市久木3丁目7-12</span></div>
        <dl>
          <dt>家賃</dt><dd>62,000円</dd>
          <dt>管理費</dt><dd>3,000円</dd>
          <dt>敷金</dt><dd>無 礼金 無</dd>
          <dt>専有面積</dt><dd>26.08㎡</dd>
          <dt>間取り</dt><dd>1K</dd>
          <dt>築年月</dt><dd>2007/05</dd>
          <dt>階層</dt><dd>1階/地上2階</dd>
          <dt>構造</dt><dd>軽量鉄骨造</dd>
          <dt>方位</dt><dd>南西</dd>
          <dt>入居可能日</dt><dd>即入居可</dd>
        </dl>
        <p>敷金 無 礼金 無</p>
      </body>
    </html>
    '''
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/8798_7details.html')
    assert p.building_name == 'アンプルール クラージュ 逗子A'
    assert p.room == '101'
    assert p.address == '神奈川県逗子市久木3丁目7-12'
    assert '賃貸物件情報' not in p.address
    assert p.deposit == '0'
    assert p.key_money == '0'
    assert p.rent == 62000
    assert p.management_fee == 3000


def test_kin_address_returns_blank_instead_of_seo_copy_when_no_physical_address():
    soup_html = '''
    <html><head><title>神奈川県、逗子市の賃貸物件情報/仲介手数料無料(不要)|木下の賃貸</title></head>
    <body><div>神奈川県、逗子市の賃貸物件情報/仲介手数料無料(不要)|木下の賃貸</div></body></html>
    '''
    from bs4 import BeautifulSoup
    from takuro_collector.extractor import _pairs
    soup = BeautifulSoup(soup_html, 'html.parser')
    got = KinoshitaAdapter._full_address(soup, _pairs(soup), '神奈川県、逗子市の賃貸物件情報/仲介手数料無料(不要)|木下の賃貸')
    assert got == ''


def test_kin_transport_accepts_quoted_station_name():
    html = '''
    <html><body>
      <h1>アムールライフタウン 201号室</h1>
      <dl>
        <dt>所在地</dt><dd>千葉県習志野市鷺沼台3-16-16</dd>
        <dt>家賃</dt><dd>75,000円</dd>
        <dt>管理費</dt><dd>3,000円</dd>
        <dt>交通</dt><dd>京成本線「京成津田沼」駅 徒歩12分</dd>
      </dl>
    </body></html>
    '''
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/11016_4details.html')
    assert p.transport
    assert p.transport[0]['line'] == '京成本線'
    assert p.transport[0]['station'] == '京成津田沼'
    assert p.transport[0]['walk_minutes'] == 12


def test_kin_transport_current_split_table_rows():
    html = r"""
    <html><body>
      <h1>アムールライフタウン 201号室</h1>
      <table>
        <tr><th>住所</th><td>千葉県習志野市鷺沼台3-16-16</td></tr>
        <tr><th>路線/バス会社</th><td>京成本線 /</td></tr>
        <tr><th>駅名/停留所名</th><td>京成大久保駅 /</td></tr>
        <tr><th>徒歩時間</th><td>京成大久保駅：14分 / 停留所：</td></tr>
      </table>
    </body></html>
    """
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/11016_4details.html')
    assert p.transport == [{
        'line': '京成本線',
        'station': '京成大久保',
        'walk_minutes': 14,
        'raw': '京成本線 京成大久保駅 徒歩14分',
    }]


def test_kin_transport_current_split_table_exports_basic_txt():
    from takuro_collector.txt_export import build_text
    html = r"""
    <html><body>
      <h1>アムールライフタウン 201号室</h1>
      <table>
        <tr><th>所在地</th><td>千葉県習志野市鷺沼台3-16-16</td></tr>
        <tr><th>家賃</th><td>75,000円</td></tr>
        <tr><th>管理費</th><td>3,000円</td></tr>
        <tr><th>路線/バス会社</th><td>京成本線 /</td></tr>
        <tr><th>駅名/停留所名</th><td>京成大久保駅 /</td></tr>
        <tr><th>徒歩時間</th><td>京成大久保駅：14分 / 停留所：</td></tr>
      </table>
    </body></html>
    """
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/11016_4details.html')
    txt = build_text(p.to_dict())
    assert '沿線名\n京成本線' in txt
    assert '駅名\n京成大久保' in txt
    assert '駅より徒歩\n14分' in txt


def test_kin_transport_ignores_station_without_time_and_keeps_timed_bus_stop():
    html = r"""
    <html><body>
      <h1>Batelira 102号室</h1>
      <table>
        <tr><th>所在地</th><td>神奈川県横浜市戸塚区深谷町715-7</td></tr>
        <tr><th>路線/バス会社</th><td>JR横須賀線 /</td></tr>
        <tr><th>駅名/停留所名</th><td>戸塚駅 / 中村三叉路停留所</td></tr>
        <tr><th>徒歩時間</th><td>戸塚駅： / 中村三叉路停留所：7分</td></tr>
      </table>
    </body></html>
    """
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/13757_2details.html')
    assert p.transport == [{
        'line': '',
        'station': '中村三叉路',
        'walk_minutes': 7,
        'raw': '中村三叉路停留所 徒歩7分',
    }]
