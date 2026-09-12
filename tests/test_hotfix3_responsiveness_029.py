from pathlib import Path


def test_manual_wp_sync_uses_chunked_state_refresh():
    source = Path('takuro_collector/ui.py').read_text(encoding='utf-8')
    assert 'def refresh_property_states_chunked' in source
    assert 'QTimer.singleShot(10, lambda: update_batch(end))' in source
    assert 'refresh_on_finish=False' in source
    assert 'self.refresh_property_states_chunked()' in source


def test_ready_errors_are_diagnostic():
    source = Path('takuro_collector/wordpress.py').read_text(encoding='utf-8')
    assert 'error_details: list[dict] = []' in source
    assert '"error_details": error_details' in source

def test_post_sync_state_refresh_uses_lightweight_db_projection():
    ui_source = Path('takuro_collector/ui.py').read_text(encoding='utf-8')
    db_source = Path('takuro_collector/db.py').read_text(encoding='utf-8')
    assert 'self.db.list_property_states(self.search.text().strip(), 500)' in ui_source
    assert 'def list_property_states' in db_source
    assert 'SELECT id,status,photo_state,pdf_path,zip_path,wp_package_state,wp_sync_state' in db_source



def test_idle_ready_poll_avoids_match_only_full_refresh():
    source = Path('takuro_collector/ui.py').read_text(encoding='utf-8')
    start = source.index('    def poll_ready_packages')
    end = source.index('    def sync_wordpress', start)
    block = source[start:end]
    assert '("downloaded", "packaged", "submitted")' in block
    assert '("matched", "downloaded", "packaged", "submitted")' not in block
    assert 'self.refresh_property_states_chunked()' in block
    assert 'QTimer.singleShot(0, self.refresh_properties)' not in block


def test_chunked_state_refresh_avoids_gui_thread_file_stats():
    source = Path('takuro_collector/ui.py').read_text(encoding='utf-8')
    start = source.index('    def refresh_property_states_chunked')
    end = source.index('    def refresh_copy_panel', start)
    block = source[start:end]
    assert 'Path(str(p["pdf_path"])).is_file()' not in block
    assert 'Path(str(p["zip_path"])).is_file()' not in block
    assert 'bool(str(p.get("pdf_path") or "").strip())' in block
    assert 'bool(str(p.get("zip_path") or "").strip())' in block
