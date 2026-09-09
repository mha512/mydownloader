from app import app


def test_admin_requires_login():
    response = app.test_client().get('/admin')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/login')


def test_admin_login_and_dashboard():
    client = app.test_client()
    login = client.post('/admin/login', data={
        'username': 'admin',
        'password': 'admin123',
    }, follow_redirects=False)
    assert login.status_code == 302
    dashboard = client.get('/admin')
    assert dashboard.status_code == 200
    assert b'Admin Dashboard' in dashboard.data


def test_admin_audit_and_reports_are_accessible():
    client = app.test_client()
    client.post('/admin/login', data={
        'username': 'admin',
        'password': 'admin123',
    }, follow_redirects=False)

    audit = client.get('/admin/audit')
    reports = client.get('/admin/reports')

    assert audit.status_code == 200
    assert b'Audit Log' in audit.data
    assert reports.status_code == 200
    assert b'Operational Reports' in reports.data
