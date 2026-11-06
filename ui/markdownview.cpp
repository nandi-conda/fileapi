#include "markdownview.h"
#include "api.h"

#include <QAbstractTextDocumentLayout>
#include <QBuffer>
#include <QImageReader>
#include <QDesktopServices>
#include <QEvent>
#include <QFontDatabase>
#include <QJsonObject>
#include <QRegularExpression>
#include <QScrollBar>
#include <QTextBlock>
#include <QTextCursor>
#include <QTextDocument>
#include <QTimer>
#include <QUrlQuery>
#include <QtMath>

namespace {
// GitHub's heading anchors: lower case, punctuation dropped, spaces become hyphens.
QString slugify(const QString &text)
{
    QString out;
    for (const QChar c : text.trimmed().toLower()) {
        if (c.isLetterOrNumber() || c == '-' || c == '_')
            out += c;
        else if (c.isSpace())
            out += '-';
    }
    return out;
}

QColor mix(const QColor &a, const QColor &b, qreal t)
{
    return QColor::fromRgbF(a.redF() + (b.redF() - a.redF()) * t, a.greenF() + (b.greenF() - a.greenF()) * t,
                            a.blueF() + (b.blueF() - a.blueF()) * t);
}

}  // namespace

MarkdownView::MarkdownView(Api *api, QWidget *parent) : QTextBrowser(parent), m_api(api)
{
    setOpenLinks(false);
    setOpenExternalLinks(false);
    setUndoRedoEnabled(false);
    setWordWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
    document()->setDocumentMargin(16);
    m_relayout = new QTimer(this);
    m_relayout->setSingleShot(true);
    m_relayout->setInterval(0);
    connect(m_relayout, &QTimer::timeout, this, [this] { relayout(); });
    connect(this, &QTextBrowser::anchorClicked, this, [this](const QUrl &url) { linkClicked(url); });
    connect(document()->documentLayout(), &QAbstractTextDocumentLayout::documentSizeChanged, this,
            [this] { fitHeight(); });
}

void MarkdownView::setAutoHeight(bool on)
{
    m_autoHeight = on;
    setVerticalScrollBarPolicy(on ? Qt::ScrollBarAlwaysOff : Qt::ScrollBarAsNeeded);
    setHorizontalScrollBarPolicy(on ? Qt::ScrollBarAlwaysOff : Qt::ScrollBarAsNeeded);
    setSizePolicy(QSizePolicy::Preferred, on ? QSizePolicy::Fixed : QSizePolicy::Expanding);
    fitHeight();
}

bool MarkdownView::isMarkdown(const QString &path)
{
    static const QRegularExpression re(QStringLiteral("\\.(md|markdown|mdown|mkd|mkdn)$"),
                                       QRegularExpression::CaseInsensitiveOption);
    return re.match(path).hasMatch();
}

QUrl MarkdownView::baseUrl() const
{
    QUrl base;
    base.setScheme(QStringLiteral("repo"));
    base.setPath("/" + m_path);
    return base;
}

void MarkdownView::setFile(const QString &repo, const QString &ref, const QString &path)
{
    ++m_gen;
    m_repo = repo;
    m_ref = ref;
    m_path = path;
    m_content.clear();
    m_images.clear();
    m_loading.clear();
    m_failed.clear();
}

void MarkdownView::showPlain(const QString &repo, const QString &ref, const QString &path, const QString &text)
{
    setFile(repo, ref, path);
    m_markdown = false;
    m_content = text;
    render();
    verticalScrollBar()->setValue(0);
}

void MarkdownView::showMarkdown(const QString &repo, const QString &ref, const QString &path, const QString &anchor,
                                std::function<void(bool ok)> done)
{
    setFile(repo, ref, path);
    m_markdown = true;
    const int gen = m_gen;
    QUrlQuery q;
    q.addQueryItem("ref", ref);
    q.addQueryItem("path", path);
    m_api->get("repos/" + repo + "/markdown", q, this, [this, gen, anchor, done](const QJsonObject &body,
                                                                                 const QString &error) {
        if (gen != m_gen)
            return;
        if (!error.isEmpty()) {
            m_markdown = false;
            m_content = QStringLiteral("Couldn't render this file: %1").arg(error);
            render();
        } else {
            m_content = body.value("html").toString();
            render();
            verticalScrollBar()->setValue(0);
            if (!anchor.isEmpty())
                QTimer::singleShot(0, this, [this, gen, anchor] {
                    if (gen == m_gen)
                        jumpTo(anchor);
                });
        }
        if (done)
            done(error.isEmpty());
    });
}

QString MarkdownView::styleSheet() const
{
    // GitHub-like look in the viewer's colours, so it follows light and dark mode.
    const QPalette pal = palette();
    const QColor text = pal.color(QPalette::Text);
    const QColor base = pal.color(QPalette::Base);
    const QString codeBg = mix(base, text, 0.07).name();
    const QString rule = mix(base, text, 0.2).name();
    const QString muted = mix(text, base, 0.35).name();
    const QString link = pal.color(QPalette::Link).name();
    return QStringLiteral(
               "a { color: %1; text-decoration: none; }"
               "code, tt, kbd, samp { background-color: %2; }"
               "pre { background-color: %2; white-space: pre-wrap; margin-top: 8px; margin-bottom: 12px; }"
               "pre code { background-color: transparent; }"
               "table { border-collapse: collapse; margin-top: 4px; margin-bottom: 12px; }"
               "th, td { border: 1px solid %3; padding: 4px 10px; }"
               "th { font-weight: bold; }"
               "blockquote { color: %4; margin-left: 16px; }"
               "h1, h2 { margin-top: 18px; margin-bottom: 8px; }"
               "h3, h4, h5, h6 { margin-top: 14px; margin-bottom: 6px; }"
               "p { margin-top: 0px; margin-bottom: 10px; }")
        .arg(link, codeBg, rule, muted);
}

void MarkdownView::render()
{
    m_rendering = true;
    const int scroll = verticalScrollBar()->value();
    QTextDocument *doc = document();
    m_fittedWidth = imageWidth();
    if (m_markdown) {
        doc->setDefaultFont(font());
        doc->setDefaultStyleSheet(styleSheet());
        doc->setBaseUrl(baseUrl());
        doc->setHtml(m_content);
        doc->setBaseUrl(baseUrl());
        decorate();
    } else {
        doc->setDefaultStyleSheet(QString());
        doc->setDefaultFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
        doc->setPlainText(m_content);
    }
    setTextCursor(QTextCursor(document()));  // at the top, or the view follows the cursor to the end
    m_rendering = false;
    verticalScrollBar()->setValue(scroll);
    fitHeight();
}

// Anchors on headings, named the way GitHub names them, so "#section" links work.
void MarkdownView::decorate()
{
    QHash<QString, int> seen;
    for (QTextBlock b = document()->begin(); b.isValid(); b = b.next()) {
        if (b.blockFormat().headingLevel() <= 0)
            continue;
        QString slug = slugify(b.text());
        const int n = seen.value(slug, 0);
        seen.insert(slug, n + 1);
        if (n > 0)
            slug += QStringLiteral("-%1").arg(n);
        QTextCursor c(b);
        c.movePosition(QTextCursor::EndOfBlock, QTextCursor::KeepAnchor);
        QTextCharFormat f;
        f.setAnchor(true);
        f.setAnchorNames({slug});
        c.mergeCharFormat(f);
    }
}

bool MarkdownView::jumpTo(const QString &anchor)
{
    if (anchor.isEmpty())
        return false;
    const QString want = anchor.toLower();
    for (QTextBlock b = document()->begin(); b.isValid(); b = b.next()) {
        for (auto it = b.begin(); !it.atEnd(); ++it) {
            if (!it.fragment().charFormat().anchorNames().contains(want))
                continue;
            const int y = qFloor(document()->documentLayout()->blockBoundingRect(b).top());
            if (m_autoHeight) {
                if (onScrollTo)
                    onScrollTo(y + frameWidth());
            } else {
                verticalScrollBar()->setValue(y);
            }
            return true;
        }
    }
    return false;
}

void MarkdownView::linkClicked(const QUrl &link)
{
    const QUrl url = baseUrl().resolved(link);
    if (url.scheme() == "repo") {
        QString path = url.path();
        while (path.startsWith('/'))
            path.remove(0, 1);
        while (path.endsWith('/'))
            path.chop(1);
        if (path == m_path && url.hasFragment())
            jumpTo(url.fragment());
        else if (onOpenPath)
            onOpenPath(path, url.fragment());
    } else if (url.scheme() == "http" || url.scheme() == "https" || url.scheme() == "mailto") {
        QDesktopServices::openUrl(url);  // a new browser tab
    }
}

int MarkdownView::imageWidth() const
{
    return std::max(64, viewport()->width() - 2 * int(document()->documentMargin()) - 8);
}

QImage MarkdownView::fitted(const QImage &image) const
{
    const int max = imageWidth();
    if (image.deviceIndependentSize().width() <= max)
        return image;
    // Wider than the view: shrink it, keeping it sharp on high-density screens.
    const qreal dpr = devicePixelRatioF();
    QImage out = image.scaledToWidth(std::min(image.width(), qRound(max * dpr)), Qt::SmoothTransformation);
    out.setDevicePixelRatio(qreal(out.width()) / max);
    return out;
}

QVariant MarkdownView::loadResource(int type, const QUrl &name)
{
    if (type != QTextDocument::ImageResource)
        return QTextBrowser::loadResource(type, name);
    if (const auto it = m_images.constFind(name); it != m_images.cend())
        return fitted(*it);
    // Until it arrives (or if it never does): an invisible placeholder rather than Qt's broken-image icon.
    QImage blank(1, 1, QImage::Format_ARGB32);
    blank.fill(Qt::transparent);
    if (m_loading.contains(name) || m_failed.contains(name) || m_repo.isEmpty())
        return blank;
    const QString scheme = name.scheme();
    if (scheme != "repo" && scheme != "http" && scheme != "https") {
        m_failed.insert(name);
        return blank;
    }
    m_loading.insert(name);
    const int gen = m_gen;
    auto done = [this, gen, name](const QByteArray &data, const QString &error) {
        if (gen != m_gen)
            return;
        m_loading.remove(name);
        QImage image;
        if (error.isEmpty()) {
            QBuffer buffer;
            buffer.setData(data);
            QImageReader reader(&buffer);
            // Vector images (SVG badges): drawn at the screen's density instead of blown up from 1x.
            const qreal dpr = devicePixelRatioF();
            const bool vector = reader.format() == "svg" || reader.format() == "svgz";
            if (vector && dpr > 1 && reader.size().isValid())
                reader.setScaledSize(reader.size() * dpr);
            image = reader.read();
            if (vector && dpr > 1)
                image.setDevicePixelRatio(dpr);
        }
        if (image.isNull()) {
            m_failed.insert(name);
            return;
        }
        m_images.insert(name, image);
        document()->addResource(QTextDocument::ImageResource, name, fitted(image));
        m_relayout->start();
    };
    if (scheme == "repo") {
        QUrlQuery q;
        q.addQueryItem("ref", m_ref);
        q.addQueryItem("path", name.path().mid(1));
        m_api->getBytes("repos/" + m_repo + "/raw", q, this, done);
    } else {
        m_api->fetchExternal(name, this, done);
    }
    return blank;
}

void MarkdownView::relayout()
{
    // New width: refit the images to it.
    if (imageWidth() != m_fittedWidth) {
        m_fittedWidth = imageWidth();
        for (auto it = m_images.cbegin(); it != m_images.cend(); ++it)
            document()->addResource(QTextDocument::ImageResource, it.key(), fitted(it.value()));
    }
    document()->markContentsDirty(0, document()->characterCount());
}

void MarkdownView::fitHeight()
{
    if (!m_autoHeight)
        return;
    const int h = qCeil(document()->documentLayout()->documentSize().height()) + 2 * frameWidth();
    if (h != height())
        setFixedHeight(h);
}

void MarkdownView::resizeEvent(QResizeEvent *event)
{
    QTextBrowser::resizeEvent(event);
    if (!m_images.isEmpty() && imageWidth() != m_fittedWidth)
        m_relayout->start();
}

void MarkdownView::changeEvent(QEvent *event)
{
    QTextBrowser::changeEvent(event);
    // Light/dark switch: the code tint and link colours come from the palette.
    if (event->type() == QEvent::PaletteChange && m_markdown && !m_rendering && !m_repo.isEmpty())
        render();
}
