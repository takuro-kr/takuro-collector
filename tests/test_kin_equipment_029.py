from takuro_collector.sites.kinoshita import KinoshitaAdapter


def base(body: str) -> str:
    return f'''<html><body>
    <h1>設備テスト 101号室</h1>
    <dl><dt>所在地</dt><dd>東京都新宿区西新宿1-1-1</dd><dt>家賃</dt><dd>80,000円</dd></dl>
    {body}
    </body></html>'''


def equipment(body: str) -> list[str]:
    return KinoshitaAdapter().parse(base(body), 'https://kinoshita-chintai.com/details/999_1details.html').equipment


def test_kin_equipment_positive_tokens_are_canonicalized():
    got = equipment('''<table>
      <tr><th>設備</th><td>バス・トイレ別○ / 室内洗濯機置き場 / ウォシュレット</td></tr>
    </table>''')
    assert 'バス・トイレ別' in got
    assert '室内洗濯機置場' in got
    assert '温水洗浄便座' in got
    assert 'ウォシュレット' not in got


def test_kin_equipment_accepts_named_positive_values():
    got = equipment('''<table>
      <tr><th>設備</th><td>バス・トイレ別 有、室内洗濯機置場 あり、温水洗浄便座 対応</td></tr>
    </table>''')
    assert {'バス・トイレ別', '室内洗濯機置場', '温水洗浄便座'} <= set(got)


def test_kin_equipment_negative_values_do_not_create_features():
    got = equipment('''<table>
      <tr><th>設備</th><td>バス・トイレ別 ×、室内洗濯機置場 無、ウォシュレット なし</td></tr>
    </table>''')
    assert 'バス・トイレ別' not in got
    assert '室内洗濯機置場' not in got
    assert '温水洗浄便座' not in got


def test_kin_equipment_does_not_scan_recommendation_or_footer():
    got = equipment('''
      <table><tr><th>設備</th><td>エアコン</td></tr></table>
      <section class="recommend"><h2>おすすめ物件</h2><p>バス・トイレ別○ 室内洗濯機置き場 ウォシュレット</p></section>
      <footer>温水洗浄便座 対応</footer>
    ''')
    assert 'バス・トイレ別' not in got
    assert '室内洗濯機置場' not in got
    assert '温水洗浄便座' not in got


def test_kin_equipment_handles_compound_laundry_label():
    got = equipment('<table><tr><th>設備</th><td>洗濯機置き場室内洗濯機置き場</td></tr></table>')
    assert '室内洗濯機置場' in got


def test_kin_equipment_excludes_duplicate_property_facts():
    got = equipment('<table><tr><th>設備</th><td>アパート, 木造, 2013, 08 築, エアコン</td></tr></table>')
    assert got == ['エアコン']
