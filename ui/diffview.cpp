#include "diffview.h"
#include "api.h"
#include "appstyle.h"
#include "highlighter.h"
#include "navpanel.h"

#include <QApplication>
#include <QComboBox>
#include <QMouseEvent>
#include <QSplitter>
#include <QSyntaxHighlighter>
#include <QTimer>
#include <QToolTip>
#include <QTreeWidget>
#include <QUrlQuery>
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

#include <tuple>

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

bool isWordChar(QChar c) { return c.isLetterOrNumber() || c == QLatin1Char('_') || c == QLatin1Char('$'); }

int toCodePoints(const QString &text, int utf16)
{
    int n = 0;
    for (int i = 0; i < utf16 && i < text.size(); ++i)
        if (!text[i].isLowSurrogate())
            ++n;
    return n;
}

bool isDark(const QPalette &pal) { return pal.color(QPalette::Base).lightness() < 128; }

QString statusWord(const QJsonObject &f)
{
    const QString s = f.value("status").toString();
    if (s == "renamed" || s == "copied")
        return QStringLiteral("%1 from %2").arg(s, f.value("old_path").toString());
    return s;
}
}  // namespace

// ---- DiffHighlighter ----------------------------------------------------------

// Colours each code line with its file's language. A hunk interleaves two versions of the file, so a
// construct left open (a block comment, a multi-line string) is tracked per side: the block state holds
// the old side's state in its low byte and the new side's above it. Headers start both sides afresh.
class DiffHighlighter : public QSyntaxHighlighter {
public:
    explicit DiffHighlighter(DiffText *view) : QSyntaxHighlighter(static_cast<QObject *>(view)), m_view(view) {}

protected:
    void highlightBlock(const QString &text) override
    {
        const int i = currentBlock().blockNumber();
        const auto &lines = m_view->lines();
        if (i >= lines.size())
            return;
        const DiffText::Line &l = lines[i];
        const int prev = std::max(0, previousBlockState());
        int oldState = prev & 0xff, newState = (prev >> 8) & 0xff;
        CodeHighlighter *engine = l.file >= 0 ? m_view->engineFor(l.file) : nullptr;
        const bool code = l.kind == DiffText::Context || l.kind == DiffText::Added || l.kind == DiffText::Removed;
        if (!code || !engine || text.isEmpty()) {
            setCurrentBlockState(code ? prev : 0);
            return;
        }
        const auto apply = [this](int start, int length, const QTextCharFormat &f) { setFormat(start + 1, length, f); };
        const QString body = text.mid(1);  // past the +/-/space marker
        // Prefer the state worked out from the whole file (see primeFile) over the running one.
        if (const int s = m_view->stateAt(l.file, true, l.oldNo); s >= 0 && l.kind != DiffText::Added)
            oldState = s;
        if (const int s = m_view->stateAt(l.file, false, l.newNo); s >= 0 && l.kind != DiffText::Removed)
            newState = s;
        if (l.kind == DiffText::Removed)
            oldState = engine->highlightLine(body, oldState, apply);
        else if (l.kind == DiffText::Added)
            newState = engine->highlightLine(body, newState, apply);
        else
            oldState = newState = engine->highlightLine(body, newState, apply);
        setCurrentBlockState((oldState & 0xff) | ((newState & 0xff) << 8));
    }

private:
    DiffText *m_view;
};

// ---- DiffText ---------------------------------------------------------------

bool DiffText::navigable(const QString &path)
{
    static const QStringList exts = {"py", "pyi", "pyw", "js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx",
                                     "go", "rs", "c", "h", "hh", "hpp", "hxx", "cc", "cpp", "cxx", "c++",
                                     "ipp", "inl", "java"};
    const QString name = path.section('/', -1);
    return name.contains('.') && exts.contains(name.section('.', -1).toLower());
}

DiffText::~DiffText()
{
    qDeleteAll(m_engines);
}

int DiffText::stateAt(int file, bool oldSide, int line) const
{
    const auto it = m_states.constFind((qint64(file) << 1) | (oldSide ? 1 : 0));
    if (it == m_states.constEnd() || line < 1 || line > it->size())
        return -1;
    return (*it)[line - 1];
}

void DiffText::primeFile(int file, bool oldSide, const QByteArray &content)
{
    CodeHighlighter *engine = m_fileEngine.value(file);
    if (!engine)
        return;
    const QStringList lines = QString::fromUtf8(content).split('\n');
    QVector<quint8> states;
    states.reserve(lines.size());
    int state = 0;
    const CodeHighlighter::FormatFn ignore = [](int, int, const QTextCharFormat &) {};
    for (const QString &l : lines) {
        states.append(quint8(state));
        state = engine->highlightLine(l.endsWith('\r') ? l.chopped(1) : l, state, ignore);
    }
    m_states.insert((qint64(file) << 1) | (oldSide ? 1 : 0), states);
    // Re-colour just this file's lines.
    const int first = m_fileBlocks.value(file, -1);
    if (first < 0)
        return;
    const int end = file + 1 < m_fileBlocks.size() ? m_fileBlocks[file + 1] : m_lines.size();
    for (QTextBlock b = document()->findBlockByNumber(first); b.isValid() && b.blockNumber() < end; b = b.next())
        m_highlighter->rehighlightBlock(b);
}

DiffText::DiffText(QWidget *parent) : QPlainTextEdit(parent), m_gutter(new DiffGutter(this))
{
    m_highlighter = new DiffHighlighter(this);
    m_hoverTimer = new QTimer(this);
    m_hoverTimer->setSingleShot(true);
    m_hoverTimer->setInterval(450);
    connect(m_hoverTimer, &QTimer::timeout, this, [this] {
        Place p;
        if (m_hover && onWordHover && placeAt(m_hoverPos, &p))
            onWordHover(p, viewport()->mapToGlobal(m_hoverPos));
    });
    viewport()->setMouseTracking(true);
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
    m_highlighter->setDocument(nullptr);  // highlight once at the end, not on every inserted line
    m_lines.clear();
    m_fileBlocks.clear();
    m_fileEngine.clear();
    m_fileNav.clear();
    if (m_commit.value("sha").toString() != m_statesFor) {
        m_states.clear();  // a new commit; a re-render of the same one (palette change) keeps them
        m_statesFor = m_commit.value("sha").toString();
    }
    const bool dark = isDark(palette());
    int fileNo = -1;
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
        m_lines.append({kind, oldNo, newNo, fileNo});
        maxNo = std::max({maxNo, oldNo, newNo});
    };

    const QJsonArray files = m_commit.value("files").toArray();
    if (files.isEmpty())
        add("No file changes in this commit.", Note, 0, 0, note);
    for (const auto &v : files) {
        const QJsonObject f = v.toObject();
        if (!m_lines.isEmpty())
            add(QString(), Spacer, 0, 0, plain);
        ++fileNo;
        {
            // One highlighter per language: the language comes from the file name.
            const QString path = f.value("path").toString();
            auto *engine = new CodeHighlighter(nullptr);
            const QString lang = engine->setFileName(path.section('/', -1));
            if (lang.isEmpty()) {
                delete engine;
                engine = nullptr;
            } else if (CodeHighlighter *known = m_engines.value(lang)) {
                delete engine;
                engine = known;
            } else {
                m_engines.insert(lang, engine);
            }
            if (engine)
                engine->setDark(dark);
            m_fileEngine.append(engine);
            m_fileNav.append(navigable(path) || navigable(f.value("old_path").toString()));
        }
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
    m_highlighter->setDocument(document());  // highlights the whole diff

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

bool DiffText::placeAt(const QPoint &pos, Place *place, QTextCursor *word) const
{
    QTextCursor c = cursorForPosition(pos);
    const QTextBlock block = c.block();
    const int n = block.blockNumber();
    if (n < 0 || n >= m_lines.size())
        return false;
    const Line &l = m_lines[n];
    if ((l.kind != Context && l.kind != Added && l.kind != Removed) || !m_fileNav.value(l.file))
        return false;
    const QString text = block.text();
    int i = c.positionInBlock();
    const QRect r = cursorRect(c);
    if (pos.x() < r.x())
        --i;  // the character under the mouse is left of the nearest gap
    if (i < 1 || i >= text.size() || !isWordChar(text[i]) || pos.y() < r.top() || pos.y() > r.bottom() + 1)
        return false;
    int start = i, end = i + 1;
    while (start > 1 && isWordChar(text[start - 1]))
        --start;
    while (end < text.size() && isWordChar(text[end]))
        ++end;
    if (text[start].isDigit())
        return false;
    place->file = l.file;
    place->oldSide = l.kind == Removed;
    place->line = l.kind == Removed ? l.oldNo : l.newNo;
    place->col = toCodePoints(text.mid(1), i - 1);
    place->word = text.mid(start, end - start);
    if (word) {
        *word = QTextCursor(block);
        word->setPosition(block.position() + start);
        word->setPosition(block.position() + end, QTextCursor::KeepAnchor);
    }
    return true;
}

void DiffText::mousePressEvent(QMouseEvent *event)
{
    m_pressed = event->button() == Qt::LeftButton;
    m_pressPos = event->position().toPoint();
    m_hoverTimer->stop();
    QToolTip::hideText();
    QPlainTextEdit::mousePressEvent(event);
}

void DiffText::mouseReleaseEvent(QMouseEvent *event)
{
    QPlainTextEdit::mouseReleaseEvent(event);
    const bool click = m_pressed && event->button() == Qt::LeftButton
        && (event->position().toPoint() - m_pressPos).manhattanLength() < QApplication::startDragDistance()
        && !textCursor().hasSelection();
    m_pressed = false;
    Place p;
    if (click && onWordClicked && placeAt(event->position().toPoint(), &p))
        onWordClicked(p);
}

void DiffText::mouseMoveEvent(QMouseEvent *event)
{
    QPlainTextEdit::mouseMoveEvent(event);
    if (event->buttons() & Qt::LeftButton)
        return;
    const QPoint pos = event->position().toPoint();
    Place p;
    QTextCursor word;
    if (!placeAt(pos, &p, &word)) {
        if (!m_hoverWord.isNull()) {
            m_hoverWord = QTextCursor();
            viewport()->setCursor(Qt::IBeamCursor);
            QToolTip::hideText();
        }
        m_hoverTimer->stop();
        return;
    }
    viewport()->setCursor(Qt::PointingHandCursor);
    m_hoverPos = pos;
    if (m_hoverWord.isNull() || m_hoverWord.selectionStart() != word.selectionStart()) {
        m_hoverWord = word;
        QToolTip::hideText();
        if (m_hover)
            m_hoverTimer->start();
    }
}

void DiffText::leaveEvent(QEvent *event)
{
    QPlainTextEdit::leaveEvent(event);
    m_hoverTimer->stop();
    m_hoverWord = QTextCursor();
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
        m_diff->setHoverEnabled(false);  // no hovering on a touch screen; a tap opens the panel
    }
    m_diff->onWordClicked = [this](const DiffText::Place &p) { lookup(p); };
    m_diff->onWordHover = [this](const DiffText::Place &p, const QPoint &at) { hover(p, at); };
    // Definitions and references of a clicked name, beside the diff (under it on a phone).
    m_split = new QSplitter;
    m_split->setChildrenCollapsible(false);
    m_split->addWidget(m_diff);
    m_nav = new NavPanel;
    m_nav->setVisible(false);
    m_nav->setBackEnabled(false);
    touchScroll(m_nav->view());
    m_nav->onOpen = [this](const QString &path, int line, int col, int end) {
        emit openPlace(m_navRef, path, line, col, end);
    };
    m_nav->onClose = [this] { closeNav(); };
    m_split->addWidget(m_nav);
    m_split->setStretchFactor(0, 3);
    m_split->setStretchFactor(1, 1);
    lay->addWidget(m_split, 1);
    setCompact(false);
}

QPair<QString, QString> CommitView::source(const DiffText::Place &p) const
{
    const QJsonObject f = m_commit.value("files").toArray().at(p.file).toObject();
    if (p.oldSide) {
        const QString old = f.value("old_path").toString();
        return {m_commit.value("parents").toArray().at(0).toString(), old.isEmpty() ? f.value("path").toString() : old};
    }
    return {m_sha, f.value("path").toString()};
}

void CommitView::primeHighlighting()
{
    const QJsonArray files = m_commit.value("files").toArray();
    const QString parent = m_commit.value("parents").toArray().at(0).toString();
    int fetches = 0;
    for (int i = 0; i < files.size() && fetches < 60; ++i) {
        const QJsonObject f = files[i].toObject();
        if (!m_diff->engineFor(i) || f.value("lfs").toBool() || f.value("hunks").toArray().isEmpty())
            continue;
        // Only a side whose hunks start part-way down the file can begin inside a comment or string.
        bool oldNeeded = false, newNeeded = false;
        for (const auto &hv : f.value("hunks").toArray()) {
            const QJsonObject h = hv.toObject();
            oldNeeded = oldNeeded || h.value("old_start").toInt() > 1;
            newNeeded = newNeeded || h.value("new_start").toInt() > 1;
        }
        const QString status = f.value("status").toString();
        const QString oldPath = f.value("old_path").toString().isEmpty() ? f.value("path").toString()
                                                                         : f.value("old_path").toString();
        const QList<std::tuple<bool, QString, QString>> sides = {
            {true, parent, oldPath}, {false, m_sha, f.value("path").toString()}};
        for (const auto &[oldSide, ref, path] : sides) {
            if (!(oldSide ? oldNeeded : newNeeded) || ref.isEmpty() || status == (oldSide ? "added" : "deleted"))
                continue;
            QUrlQuery q;
            q.addQueryItem("ref", ref);
            q.addQueryItem("path", path);
            const int seq = m_seq, file = i;
            const bool old = oldSide;
            ++fetches;
            m_api->getBytes("repos/" + m_repo + "/raw", q, this, [this, seq, file, old](const QByteArray &data, const QString &error) {
                if (seq == m_seq && error.isEmpty() && data.size() <= 2 * 1024 * 1024)
                    m_diff->primeFile(file, old, data);
            });
        }
    }
}

void CommitView::closeNav()
{
    ++m_navSeq;
    m_nav->setVisible(false);
}

void CommitView::lookup(const DiffText::Place &p)
{
    const auto src = source(p);
    const QString ref = src.first, path = src.second;
    if (ref.isEmpty())
        return;
    const int seq = ++m_navSeq;
    const bool wasHidden = !m_nav->isVisible();
    QToolTip::hideText();
    m_nav->setVisible(true);
    m_nav->setLoading(p.word);
    emit statusText("Looking up " + p.word + "\u2026", false);
    QUrlQuery q;
    q.addQueryItem("ref", ref);
    q.addQueryItem("path", path);
    q.addQueryItem("line", QString::number(p.line));
    q.addQueryItem("col", QString::number(p.col));
    m_api->get("repos/" + m_repo + "/symbol", q, this, [this, seq, ref, wasHidden](const QJsonObject &r, const QString &error) {
        if (seq != m_navSeq)
            return;
        if (!error.isEmpty()) {
            m_nav->setError(error);
            return emit statusText("Lookup failed: " + error, true);
        }
        if (r.value("symbol").isNull()) {
            closeNav();
            return emit statusText("Nothing to look up there.", false);
        }
        m_navRef = ref;
        m_nav->setResult(r);
        if (m_split->orientation() == Qt::Vertical) {
            if (wasHidden || m_split->sizes().value(1) < 120)
                m_split->setSizes({m_split->height() / 2, m_split->height() / 2});
        } else if (m_split->sizes().value(1) < 120) {
            m_split->setSizes({std::max(300, m_split->width() * 2 / 3), std::max(240, m_split->width() / 3)});
        }
        const QString name = r.value("symbol").toObject().value("name").toString();
        const QJsonArray refs = r.value("references").toArray();
        emit statusText(QStringLiteral("%1: %2 definition(s), %3 reference(s) at %4")
                            .arg(name).arg(r.value("definitions").toArray().size())
                            .arg(r.value("reference_count").toInt(refs.size())).arg(ref.left(7)), false);
    });
}

void CommitView::hover(const DiffText::Place &p, const QPoint &globalPos)
{
    const auto src = source(p);
    const QString ref = src.first, path = src.second;
    if (ref.isEmpty())
        return;
    const auto show = [this, globalPos](const QJsonObject &r) {
        const QJsonArray defs = r.value("definitions").toArray();
        if (defs.isEmpty())
            return;
        const QJsonObject d = defs.first().toObject();
        QString html = "<pre style='margin:0'>" + d.value("signature").toString().toHtmlEscaped() + "</pre>";
        const QString doc = d.value("doc").toString();
        if (!doc.isEmpty()) {
            QString shortDoc = doc.left(800);
            if (shortDoc.size() < doc.size())
                shortDoc += "\u2026";
            html += "<p style='white-space:pre-wrap; margin-top:6px'>" + shortDoc.toHtmlEscaped() + "</p>";
        }
        QString where = QStringLiteral("%1:%2").arg(d.value("path").toString()).arg(d.value("line").toInt());
        if (defs.size() > 1)
            where += QStringLiteral(" \u00b7 and %1 more").arg(defs.size() - 1);
        html += "<p style='margin-top:6px'><i>" + where.toHtmlEscaped() + "</i> &nbsp;\u00b7&nbsp; click for references</p>";
        QToolTip::showText(globalPos, html, m_diff);
    };
    for (const QJsonObject &r : std::as_const(m_hoverCache)) {
        const QJsonObject s = r.value("symbol").toObject();
        if (r.value("_ref").toString() == ref && r.value("_path").toString() == path
            && s.value("line").toInt() == p.line && s.value("col").toInt() <= p.col && p.col < s.value("end_col").toInt())
            return show(r);
    }
    const int seq = ++m_hoverSeq;
    QUrlQuery q;
    q.addQueryItem("ref", ref);
    q.addQueryItem("path", path);
    q.addQueryItem("line", QString::number(p.line));
    q.addQueryItem("col", QString::number(p.col));
    q.addQueryItem("references", "false");
    const int commitSeq = m_seq;
    m_api->get("repos/" + m_repo + "/symbol", q, this, [this, seq, commitSeq, ref, path, show](QJsonObject r, const QString &error) {
        if (!error.isEmpty() || commitSeq != m_seq || r.value("symbol").isNull())
            return;
        r.insert("_ref", ref);
        r.insert("_path", path);
        if (m_hoverCache.size() > 200)
            m_hoverCache.clear();
        m_hoverCache.append(r);
        if (seq == m_hoverSeq && m_diff->underMouse())
            show(r);
    });
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
    m_split->setOrientation(compact ? Qt::Vertical : Qt::Horizontal);
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
    m_hoverCache.clear();
    closeNav();
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
    primeHighlighting();
    bool nav = false;
    for (const auto &v : c.value("files").toArray())
        nav = nav || DiffText::navigable(v.toObject().value("path").toString());
    emit statusText(QStringLiteral("Commit %1").arg(m_sha.left(12))
                        + (!nav ? QString() : isTouchUi() ? QStringLiteral(" \u00b7 tap a name for its definition and references")
                                                          : QStringLiteral(" \u00b7 click a name for its definition and references")),
                    false);
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
