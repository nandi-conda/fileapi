#include "diffview.h"
#include "api.h"
#include "appstyle.h"

#include <QComboBox>
#include <QDateTime>
#include <QFontDatabase>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QLocale>
#include <QPainter>
#include <QPushButton>
#include <QScrollBar>
#include <QStyle>
#include <QTextBlock>
#include <QTextCursor>
#include <QToolButton>
#include <QVBoxLayout>

namespace {
class DiffGutter : public QWidget {
public:
    explicit DiffGutter(DiffText *view) : QWidget(view), m_view(view) {}
    QSize sizeHint() const override { return {m_view->gutterWidth(), 0}; }

protected:
    void paintEvent(QPaintEvent *event) override { m_view->paintGutter(event); }

private:
    DiffText *m_view;
};

constexpr int Pad = 6;          // px around each number column
constexpr int BodyLines = 4;    // commit message lines shown before "Show more"

bool isDark(const QPalette &pal) { return pal.color(QPalette::Base).lightness() < 128; }

QString statusWord(const QJsonObject &f)
{
    const QString s = f.value("status").toString();
    if (s == "renamed" || s == "copied")
        return QStringLiteral("%1 from %2").arg(s, f.value("old_path").toString());
    return s;
}
}  // namespace

// ---- DiffText ---------------------------------------------------------------

DiffText::DiffText(QWidget *parent) : QPlainTextEdit(parent), m_gutter(new DiffGutter(this))
{
    setReadOnly(true);
    setLineWrapMode(QPlainTextEdit::NoWrap);
    setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    setTabStopDistance(fontMetrics().horizontalAdvance(QLatin1Char(' ')) * 4);
    connect(this, &QPlainTextEdit::updateRequest, this, [this](const QRect &r, int dy) {
        if (dy)
            m_gutter->scroll(0, dy);
        else
            m_gutter->update(0, r.y(), m_gutter->width(), r.height());
    });
    setViewportMargins(gutterWidth(), 0, 0, 0);
}

int DiffText::gutterWidth() const
{
    return 2 * (2 * Pad + fontMetrics().horizontalAdvance(QLatin1Char('9')) * m_digits);
}

void DiffText::resizeEvent(QResizeEvent *event)
{
    QPlainTextEdit::resizeEvent(event);
    const QRect cr = contentsRect();
    m_gutter->setGeometry(QRect(cr.left(), cr.top(), gutterWidth(), cr.height()));
}

void DiffText::changeEvent(QEvent *event)
{
    QPlainTextEdit::changeEvent(event);
    if (event->type() == QEvent::PaletteChange && !m_commit.isEmpty())
        render();  // the line colours depend on light or dark mode
    if (event->type() == QEvent::FontChange) {
        setViewportMargins(gutterWidth(), 0, 0, 0);
        setTabStopDistance(fontMetrics().horizontalAdvance(QLatin1Char(' ')) * 4);
    }
}

QColor DiffText::background(Kind kind) const
{
    const bool dark = isDark(palette());
    const QColor base = palette().color(QPalette::Base);
    switch (kind) {
    case Added:      return dark ? QColor(0x1d, 0x3b, 0x27) : QColor(0xe6, 0xff, 0xec);
    case Removed:    return dark ? QColor(0x45, 0x1f, 0x22) : QColor(0xff, 0xeb, 0xe9);
    case HunkHeader: return dark ? QColor(0x1a, 0x2c, 0x44) : QColor(0xdd, 0xf4, 0xff);
    case FileHeader: return dark ? base.lighter(150) : base.darker(108);
    default:         return QColor();
    }
}

void DiffText::setDiff(const QJsonObject &commit)
{
    m_commit = commit;
    render();
    verticalScrollBar()->setValue(0);
    horizontalScrollBar()->setValue(0);
}

void DiffText::render()
{
    const int scroll = verticalScrollBar()->value();
    m_lines.clear();
    m_fileBlocks.clear();
    int maxNo = 0;

    const QColor text = palette().color(QPalette::Text);
    QColor muted = text;
    muted.setAlphaF(0.6f);
    QTextCharFormat plain;
    QTextCharFormat bold;
    bold.setFontWeight(QFont::Bold);
    QTextCharFormat hunkFmt;
    hunkFmt.setForeground(isDark(palette()) ? QColor(0x8b, 0xb4, 0xf0) : QColor(0x0a, 0x50, 0xa0));
    QTextCharFormat note;
    note.setForeground(muted);
    note.setFontItalic(true);

    clear();
    QTextCursor c(document());
    c.beginEditBlock();
    bool first = true;
    auto add = [&](const QString &s, Kind kind, int oldNo, int newNo, const QTextCharFormat &fmt) {
        QTextBlockFormat bf;
        const QColor bg = background(kind);
        if (bg.isValid())
            bf.setBackground(bg);
        if (kind == FileHeader) {
            bf.setTopMargin(2);
            bf.setBottomMargin(2);
        }
        if (first) {
            c.setBlockFormat(bf);
            c.setBlockCharFormat(fmt);
            first = false;
        } else {
            c.insertBlock(bf, fmt);
        }
        c.insertText(s, fmt);
        m_lines.append({kind, oldNo, newNo});
        maxNo = std::max({maxNo, oldNo, newNo});
    };

    const QJsonArray files = m_commit.value("files").toArray();
    if (files.isEmpty())
        add("No file changes in this commit.", Note, 0, 0, note);
    for (const auto &v : files) {
        const QJsonObject f = v.toObject();
        if (!m_lines.isEmpty())
            add(QString(), Spacer, 0, 0, plain);
        m_fileBlocks.append(m_lines.size());
        QString head = f.value("path").toString();
        const QString status = f.value("status").toString();
        if (status != "modified")
            head += "  (" + statusWord(f) + ")";
        const int plus = f.value("additions").toInt(), minus = f.value("deletions").toInt();
        if (plus || minus)
            head += QStringLiteral("  +%1 −%2").arg(plus).arg(minus);
        add(head, FileHeader, 0, 0, bold);
        if (f.contains("old_mode"))
            add(QStringLiteral("mode %1 → %2").arg(f.value("old_mode").toString(), f.value("new_mode").toString()),
                Note, 0, 0, note);
        const QString skipped = f.value("skipped").toString();
        if (!skipped.isEmpty()) {
            QString why = skipped == "binary"       ? QStringLiteral("Binary file not shown.")
                          : skipped == "too large"  ? QStringLiteral("File too large to show.")
                          : skipped == "submodule"  ? QStringLiteral("Submodule change not shown.")
                                                    : QStringLiteral("Not shown: the diff is too big.");
            if (f.value("lfs").toBool())
                why = "Stored with Git LFS. " + why;
            add(why, Note, 0, 0, note);
            continue;
        }
        const QJsonArray hunks = f.value("hunks").toArray();
        if (hunks.isEmpty())
            add(status == "renamed" ? QStringLiteral("Renamed without changes.") : QStringLiteral("No content changes."),
                Note, 0, 0, note);
        for (const auto &hv : hunks) {
            const QJsonObject h = hv.toObject();
            int o = h.value("old_start").toInt(), n = h.value("new_start").toInt();
            add(QStringLiteral("@@ -%1,%2 +%3,%4 @@").arg(o).arg(h.value("old_lines").toInt()).arg(n)
                    .arg(h.value("new_lines").toInt()),
                HunkHeader, 0, 0, hunkFmt);
            o = std::max(o, 1);
            n = std::max(n, 1);
            for (const auto &lv : h.value("lines").toArray()) {
                const QString s = lv.toString();
                const QChar k = s.isEmpty() ? QLatin1Char(' ') : s[0];
                if (k == QLatin1Char('+'))
                    add(s, Added, 0, n++, plain);
                else if (k == QLatin1Char('-'))
                    add(s, Removed, o++, 0, plain);
                else
                    add(s, Context, o++, n++, plain);
            }
        }
    }
    if (m_commit.value("truncated").toBool())
        add("This commit is too big to show in full.", Note, 0, 0, note);
    c.endEditBlock();

    int digits = 1;
    for (int x = std::max(1, maxNo); x >= 10; x /= 10)
        ++digits;
    m_digits = std::max(digits, isTouchUi() ? 2 : 3);
    setViewportMargins(gutterWidth(), 0, 0, 0);
    const QRect cr = contentsRect();
    m_gutter->setGeometry(QRect(cr.left(), cr.top(), gutterWidth(), cr.height()));
    m_gutter->update();
    verticalScrollBar()->setValue(scroll);
}

void DiffText::scrollToBlock(int block)
{
    const QTextBlock b = document()->findBlockByNumber(block);
    if (!b.isValid())
        return;
    QTextCursor c(b);
    setTextCursor(c);
    // Put the file header at the top, not wherever ensureCursorVisible would leave it.
    verticalScrollBar()->setValue(verticalScrollBar()->maximum());
    setTextCursor(c);
    ensureCursorVisible();
    horizontalScrollBar()->setValue(0);
}

void DiffText::paintGutter(QPaintEvent *event)
{
    QPainter p(m_gutter);
    const QPalette pal = palette();
    const QColor base = pal.color(QPalette::Base);
    const bool dark = isDark(pal);
    p.fillRect(event->rect(), dark ? base.lighter(125) : base.darker(104));
    QColor num = pal.color(QPalette::Text);
    num.setAlphaF(0.5f);
    p.setPen(num);
    p.setFont(font());
    const int col = gutterWidth() / 2;

    QTextBlock block = firstVisibleBlock();
    int top = qRound(blockBoundingGeometry(block).translated(contentOffset()).top());
    while (block.isValid() && top <= event->rect().bottom()) {
        const int h = qRound(blockBoundingRect(block).height());
        const int i = block.blockNumber();
        if (block.isVisible() && top + h >= event->rect().top() && i < m_lines.size()) {
            const Line &l = m_lines[i];
            QColor bg = background(l.kind);
            if (bg.isValid()) {
                if (l.kind == Added || l.kind == Removed)
                    bg = dark ? bg.lighter(125) : bg.darker(104);
                p.fillRect(0, top, m_gutter->width(), h, bg);
            }
            const int lineH = fontMetrics().height();
            if (l.oldNo)
                p.drawText(0, top, col - Pad, lineH, Qt::AlignRight, QString::number(l.oldNo));
            if (l.newNo)
                p.drawText(col, top, col - Pad, lineH, Qt::AlignRight, QString::number(l.newNo));
        }
        top += h;
        block = block.next();
    }
}

// ---- CommitView -------------------------------------------------------------

CommitView::CommitView(Api *api, QWidget *parent) : QWidget(parent), m_api(api)
{
    auto *lay = new QVBoxLayout(this);
    lay->setContentsMargins(0, 4, 0, 0);
    lay->setSpacing(4);

    auto *top = new QHBoxLayout;
    auto *back = new QToolButton;
    back->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    back->setText("Commits");
    back->setToolTip("Back to the list of commits");
    back->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    back->setAutoRaise(true);
    connect(back, &QToolButton::clicked, this, &CommitView::back);
    top->addWidget(back);
    top->addStretch(1);
    m_browse = new QPushButton("Browse files");
    m_browse->setIcon(style()->standardIcon(QStyle::SP_DirIcon));
    m_browse->setToolTip("Browse the repository's files as they were at this commit");
    connect(m_browse, &QPushButton::clicked, this, [this] { emit browseFiles(m_sha); });
    top->addWidget(m_browse);
    lay->addLayout(top);

    m_title = new QLabel;
    m_title->setWordWrap(true);
    m_title->setTextInteractionFlags(Qt::TextSelectableByMouse);
    lay->addWidget(m_title);

    m_bodyLabel = new QLabel;
    m_bodyLabel->setWordWrap(true);
    m_bodyLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
    m_bodyLabel->setTextFormat(Qt::PlainText);
    lay->addWidget(m_bodyLabel);
    m_more = new QToolButton;
    m_more->setAutoRaise(true);
    m_more->setCheckable(true);
    connect(m_more, &QToolButton::toggled, this, [this](bool on) { setBodyExpanded(on); });
    lay->addWidget(m_more);

    m_meta = new QLabel;
    m_meta->setWordWrap(true);
    m_meta->setTextFormat(Qt::RichText);
    connect(m_meta, &QLabel::linkActivated, this, [this](const QString &link) { emit openCommit(link); });
    lay->addWidget(m_meta);

    auto *filesRow = new QHBoxLayout;
    m_stats = new QLabel;
    filesRow->addWidget(m_stats);
    m_files = new QComboBox;
    m_files->setToolTip("Jump to a changed file");
    m_files->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_files->setMinimumContentsLength(12);
    connect(m_files, &QComboBox::activated, this, [this](int i) {
        if (i > 0 && i - 1 < m_diff->fileBlocks().size())
            m_diff->scrollToBlock(m_diff->fileBlocks()[i - 1]);
    });
    filesRow->addWidget(m_files, 1);
    lay->addLayout(filesRow);

    m_diff = new DiffText;
    if (isTouchUi()) {
        m_diff->setTextInteractionFlags(Qt::TextSelectableByKeyboard);
        m_diff->setCursorWidth(0);
        touchScroll(m_diff);
    }
    lay->addWidget(m_diff, 1);
    setCompact(false);
}

void CommitView::setRepo(const QString &repo)
{
    m_repo = repo;
    m_sha.clear();
    ++m_seq;
}

void CommitView::setCompact(bool compact)
{
    // A phone is too narrow to scroll sideways through long lines: wrap them instead.
    m_diff->setLineWrapMode(compact ? QPlainTextEdit::WidgetWidth : QPlainTextEdit::NoWrap);
    QFont mono = QFontDatabase::systemFont(QFontDatabase::FixedFont);
    if (compact)
        mono.setPointSizeF(mono.pointSizeF() * 0.85);
    m_diff->setFont(mono);
    m_browse->setText(compact ? "Files" : "Browse files");
    // A phone has little height to spare above the diff: a smaller title, and the message body only on request.
    m_compact = compact;
    QFont tf = font();
    tf.setBold(true);
    tf.setPointSizeF(tf.pointSizeF() * (compact ? 1.0 : 1.15));
    m_title->setFont(tf);
    setBodyExpanded(m_more->isChecked());
    if (!m_commit.isEmpty())
        fillStats();
}

void CommitView::showCommit(const QString &sha)
{
    if (sha == m_sha || (!m_sha.isEmpty() && m_sha.startsWith(sha) && sha.size() >= 7))
        return;  // already showing it
    m_sha = sha;
    const int seq = ++m_seq;
    m_title->setText("Loading commit " + sha.left(12) + "…");
    m_body.clear();
    m_commit = {};
    m_bodyLabel->hide();
    m_more->hide();
    m_meta->clear();
    m_stats->clear();
    m_files->clear();
    m_browse->setEnabled(false);
    m_diff->setDiff({});
    m_api->get("repos/" + m_repo + "/commits/" + sha, {}, this, [this, seq, sha](const QJsonObject &body, const QString &error) {
        if (seq != m_seq)
            return;
        if (!error.isEmpty()) {
            m_title->setText("Could not load commit " + sha.left(12));
            m_meta->setText(error.toHtmlEscaped());
            emit statusText("Could not load commit: " + error, true);
            return;
        }
        fill(body);
    });
}

void CommitView::fill(const QJsonObject &c)
{
    m_sha = c.value("sha").toString();
    const QString msg = c.value("message").toString();
    m_title->setText(msg.section('\n', 0, 0));
    m_body = msg.section('\n', 1).trimmed();
    m_more->setChecked(false);
    setBodyExpanded(false);

    const QString author = c.value("author").toString().section(" <", 0, 0);
    const QDateTime when = QDateTime::fromString(c.value("date").toString(), Qt::ISODate);
    QString meta = QStringLiteral("<b>%1</b> committed %2 &middot; <code>%3</code>")
                       .arg(author.toHtmlEscaped(), QLocale().toString(when.toLocalTime(), QLocale::ShortFormat),
                            m_sha.left(10));
    const QJsonArray parents = c.value("parents").toArray();
    QStringList links;
    for (const auto &p : parents)
        links << QStringLiteral("<a href=\"%1\"><code>%2</code></a>").arg(p.toString(), p.toString().left(7));
    if (!links.isEmpty())
        meta += QStringLiteral(" &middot; %1 %2").arg(links.size() > 1 ? "parents" : "parent", links.join(", "));
    m_meta->setText(meta);

    m_commit = c;
    fillStats();
    m_files->clear();
    m_files->addItem("Jump to file…");
    for (const auto &v : c.value("files").toArray()) {
        const QJsonObject f = v.toObject();
        m_files->addItem(QStringLiteral("%1  +%2 −%3").arg(f.value("path").toString())
                             .arg(f.value("additions").toInt()).arg(f.value("deletions").toInt()));
    }
    m_files->setEnabled(m_files->count() > 1);
    m_browse->setEnabled(true);
    m_diff->setDiff(c);
    emit statusText(QStringLiteral("Commit %1").arg(m_sha.left(12)), false);
}

void CommitView::fillStats()
{
    const QJsonObject st = m_commit.value("stats").toObject();
    const int n = st.value("files").toInt();
    m_stats->setText(QStringLiteral("%1 file%2%3 <span style=\"color:#2da44e\">+%4</span> "
                                    "<span style=\"color:#cf222e\">−%5</span>")
                         .arg(n).arg(n == 1 ? "" : "s").arg(m_compact ? "" : " changed,")
                         .arg(st.value("additions").toInt()).arg(st.value("deletions").toInt()));
}

void CommitView::setBodyExpanded(bool expanded)
{
    const int limit = m_compact ? 0 : BodyLines;
    const QStringList lines = m_body.split('\n');
    const bool longBody = !m_body.isEmpty() && (lines.size() > limit || m_body.size() > 400);
    m_more->setVisible(longBody);
    m_more->setText(expanded ? "Show less" : limit == 0 ? "Show message" : "Show more");
    m_bodyLabel->setVisible(!m_body.isEmpty() && (expanded || limit > 0));
    if (expanded || !longBody)
        m_bodyLabel->setText(m_body);
    else
        m_bodyLabel->setText(lines.mid(0, limit).join('\n').left(400) + "…");
}
