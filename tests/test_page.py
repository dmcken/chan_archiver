from chan_archiver.page import comment_text, external_links, rewrite_html, static_assets

HTML = """
<link rel="stylesheet" href="//s.4cdn.org/css/yotsubluemobile.715.css">
<link rel="stylesheet" href="//s.4cdn.org/css/yotsubluemobile.715.css">
<script src="//s.4cdn.org/js/core.min.1234.js"></script>
<a href="//i.4cdn.org/w/1700000000000.jpg"><img src="//i.4cdn.org/w/1700000000000s.jpg"></a>
"""


def test_static_assets():
    assert static_assets(HTML) == (["yotsubluemobile.715.css"], ["core.min.1234.js"])


def test_rewrite_html():
    out = rewrite_html(HTML)
    assert 'href="css/yotsubluemobile.715.css"' in out
    assert 'src="js/core.min.1234.js"' in out
    assert 'href="images/1700000000000.jpg"' in out
    assert 'src="thumbs/1700000000000s.jpg"' in out
    assert "4cdn.org" not in out


def test_comment_text():
    assert comment_text("a<br>b &amp; c<wbr>d <b>e</b>") == "a\nb & cd e"


def test_external_links():
    posts = [
        {"com": "see https://exa<wbr>mple.com/x?a=1&amp;b=2<br>and www.foo.org"},
        {"no": 2},
        {"com": "nothing here"},
        {"com": "ftp://files.example.net/f"},
    ]
    assert external_links(posts) == [
        "https://example.com/x?a=1&b=2",
        "www.foo.org",
        "ftp://files.example.net/f",
    ]
