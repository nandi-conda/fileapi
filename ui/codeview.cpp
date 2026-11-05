#include "codeview.h"

#include <QApplication>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QPainter>
#include <QScrollBar>
#include <QTextBlock>
#include <QTimer>
#include <QToolTip>

namespace {
class Gutter : public QWidget {
public:
    explicit Gutter(CodeView *view) : QWidget(view), m_view(view) {}
    QSize sizeHint() const override { return {m_view->gutterWidth(), 0}; }

protected:
    void paintEvent(QPaintEvent *event) override { m_view->paintGutter(event); }
    void mousePressEvent(QMouseEvent *event) override { m_view->gutterClicked(event); }

private:
    CodeView *m_view;
};

constexpr int GutterPad = 8;  // px left and right of the numbers

bool isWordChar(QChar c) { return c.isLetterOrNumber() || c == QLatin1Char('_') || c == QLatin1Char('$'); }
}  // namespace

CodeView::CodeView(QWidget *parent) : QPlainTextEdit(parent), m_gutter(new Gutter(this))
{
    connect(this, &QPlainTextEdit::blockCountChanged, this, [this] { updateGutterWidth(); });
    connect(this, &QPlainTextEdit::updateRequest, this, [this](const QRect &r, int dy) { updateGutter(r, dy); });
    updateGutterWidth();
    m_gutter->setCursor(Qt::PointingHandCursor);
    m_gutter->setToolTip(QStringLiteral("Click a line number to link to it; shift-click to link a range"));

    m_hoverTimer = new QTimer(this);
    m_hoverTimer->setSingleShot(true);
    m_hoverTimer->setInterval(450);
    connect(m_hoverTimer, &QTimer::timeout, this, [this] {
        int line, col;
        if (m_nav && onWordHover && wordAt(m_hoverPos, &line, &col))
            onWordHover(line, col, viewport()->mapToGlobal(m_hoverPos));
    });
    viewport()->setMouseTracking(true);
}

void CodeView::setNavEnabled(bool on)
{
    m_nav = on;
    m_hoverTimer->stop();
    m_hoverWord = QTextCursor();
    viewport()->setCursor(Qt::IBeamCursor);
    if (!on)
        setSymbolMarks({});
}

bool CodeView::wordAt(const QPoint &pos, int *line, int *col, QTextCursor *word) const
{
    QTextCursor c = cursorForPosition(pos);
    const QTextBlock block = c.block();
    const QString text = block.text();
    int i = c.positionInBlock();
    // cursorForPosition gives the nearest gap between characters: the character under the mouse is
    // the one before that gap when the mouse is left of it.
    const QRect r = cursorRect(c);
    if (pos.x() < r.x())
        --i;
    if (i < 0 || i >= text.size() || !isWordChar(text[i]))
        return false;
    if (pos.y() < r.top() || pos.y() > r.bottom() + 1)
        return false;
    int start = i, end = i + 1;
    while (start > 0 && isWordChar(text[start - 1]))
        --start;
    while (end < text.size() && isWordChar(text[end]))
        ++end;
    if (text[start].isDigit())
        return false;  // a number, not a name
    *line = block.blockNumber() + 1;
    *col = i;
    if (word) {
        *word = QTextCursor(block);
        word->setPosition(block.position() + start);
        word->setPosition(block.position() + end, QTextCursor::KeepAnchor);
    }
    return true;
}

void CodeView::mousePressEvent(QMouseEvent *event)
{
    m_pressed = event->button() == Qt::LeftButton;
    m_pressPos = event->position().toPoint();
    m_hoverTimer->stop();
    QToolTip::hideText();
    QPlainTextEdit::mousePressEvent(event);
}

void CodeView::mouseReleaseEvent(QMouseEvent *event)
{
    QPlainTextEdit::mouseReleaseEvent(event);
    const bool click = m_pressed && event->button() == Qt::LeftButton
        && (event->position().toPoint() - m_pressPos).manhattanLength() < QApplication::startDragDistance()
        && !textCursor().hasSelection();
    m_pressed = false;
    int line, col;
    if (click && m_nav && onWordClicked && wordAt(event->position().toPoint(), &line, &col)) {
        const bool jump = event->modifiers() & (Qt::ControlModifier | Qt::MetaModifier);
        onWordClicked(line, col, jump);
    }
}

void CodeView::mouseMoveEvent(QMouseEvent *event)
{
    QPlainTextEdit::mouseMoveEvent(event);
    if (!m_nav || (event->buttons() & Qt::LeftButton))
        return;
    const QPoint pos = event->position().toPoint();
    int line, col;
    QTextCursor word;
    if (!wordAt(pos, &line, &col, &word)) {
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
        m_hoverWord = word;  // a new word: rest on it a moment before asking the server
        QToolTip::hideText();
        m_hoverTimer->start();
    }
}

void CodeView::leaveEvent(QEvent *event)
{
    QPlainTextEdit::leaveEvent(event);
    m_hoverTimer->stop();
    m_hoverWord = QTextCursor();
}

void CodeView::keyPressEvent(QKeyEvent *event)
{
    if (m_nav && onNavKey && event->key() == Qt::Key_F12) {
        const QTextCursor c = textCursor();
        onNavKey(c.blockNumber() + 1, c.positionInBlock(), event->modifiers() & Qt::ShiftModifier);
        return;
    }
    QPlainTextEdit::keyPressEvent(event);
}

void CodeView::setSymbolMarks(const QList<Span> &spans)
{
    m_symbols = spans;
    applyMarks();
}

void CodeView::scrollToLine(int line)
{
    const int lineH = fontMetrics().lineSpacing();
    const int above = std::max(0, viewport()->height() / 3 / std::max(1, lineH));
    verticalScrollBar()->setValue(std::max(0, line - 1 - above));
}

void CodeView::revealSpan(int line, int col, int endCol)
{
    const QTextBlock block = document()->findBlockByNumber(line - 1);
    if (!block.isValid())
        return;
    QTextCursor c(block);
    c.setPosition(block.position() + std::clamp(col, 0, int(block.length() - 1)));
    c.setPosition(block.position() + std::clamp(endCol, 0, int(block.length() - 1)), QTextCursor::KeepAnchor);
    setTextCursor(c);
    scrollToLine(line);
    horizontalScrollBar()->setValue(0);
    ensureCursorVisible();
}

void CodeView::gutterClicked(QMouseEvent *event)
{
    if (event->button() != Qt::LeftButton)
        return;
    const int line = cursorForPosition(QPoint(0, int(event->position().y()))).blockNumber() + 1;
    int first = line, last = line;
    if ((event->modifiers() & Qt::ShiftModifier) && m_anchor > 0) {
        first = std::min(m_anchor, line);
        last = std::max(m_anchor, line);
    } else {
        m_anchor = line;
    }
    setMarkedLines(first, last, false);
    if (onLinesClicked)
        onLinesClicked(first, last);
}

void CodeView::setMarkedLines(int first, int last, bool scroll)
{
    const int count = blockCount();
    if (first <= 0 || first > count) {
        m_first = m_last = m_anchor = 0;
    } else {
        m_first = first;
        m_last = std::clamp(std::max(first, last), first, count);
        if (scroll) {
            m_anchor = m_first;
            // Put the first marked line about a third of the way down, like GitHub does.
            QTextCursor c(document()->findBlockByNumber(m_first - 1));
            setTextCursor(c);
            scrollToLine(m_first);
        }
    }
    applyMarks();
}

void CodeView::applyMarks()
{
    QList<QTextEdit::ExtraSelection> marks;
    if (m_first > 0) {
        QColor bg = palette().color(QPalette::Highlight);
        bg.setAlphaF(0.22f);
        for (int n = m_first; n <= m_last; ++n) {
            QTextEdit::ExtraSelection sel;
            sel.cursor = QTextCursor(document()->findBlockByNumber(n - 1));
            sel.format.setBackground(bg);
            sel.format.setProperty(QTextFormat::FullWidthSelection, true);
            marks.append(sel);
        }
    }
    // Occurrences of the symbol last clicked: a soft box behind each, like an editor's "highlight references".
    QColor sym = palette().color(QPalette::Highlight);
    sym.setAlphaF(0.30f);
    for (const Span &s : std::as_const(m_symbols)) {
        const QTextBlock block = document()->findBlockByNumber(s.line - 1);
        if (!block.isValid())
            continue;
        QTextEdit::ExtraSelection sel;
        sel.cursor = QTextCursor(block);
        sel.cursor.setPosition(block.position() + std::min(s.col, block.length() - 1));
        sel.cursor.setPosition(block.position() + std::min(s.endCol, block.length() - 1), QTextCursor::KeepAnchor);
        sel.format.setBackground(sym);
        marks.append(sel);
    }
    setExtraSelections(marks);
    m_gutter->update();
}

int CodeView::gutterWidth() const
{
    int digits = 1;
    for (int n = std::max(1, blockCount()); n >= 10; n /= 10)
        ++digits;
    digits = std::max(digits, 3);  // keep short files from looking cramped
    return 2 * GutterPad + fontMetrics().horizontalAdvance(QLatin1Char('9')) * digits;
}

void CodeView::updateGutterWidth()
{
    setViewportMargins(gutterWidth(), 0, 0, 0);
}

void CodeView::updateGutter(const QRect &rect, int dy)
{
    if (dy)
        m_gutter->scroll(0, dy);
    else
        m_gutter->update(0, rect.y(), m_gutter->width(), rect.height());
    if (rect.contains(viewport()->rect()))
        updateGutterWidth();
}

void CodeView::resizeEvent(QResizeEvent *event)
{
    QPlainTextEdit::resizeEvent(event);
    const QRect cr = contentsRect();
    m_gutter->setGeometry(QRect(cr.left(), cr.top(), gutterWidth(), cr.height()));
}

void CodeView::changeEvent(QEvent *event)
{
    QPlainTextEdit::changeEvent(event);
    if (event->type() == QEvent::FontChange)
        updateGutterWidth();
    if (event->type() == QEvent::PaletteChange)
        applyMarks();
}

void CodeView::paintGutter(QPaintEvent *event)
{
    QPainter p(m_gutter);
    const QPalette pal = palette();
    // A shade off the text background, so the gutter reads as separate in light and dark mode.
    const QColor base = pal.color(QPalette::Base);
    const bool dark = base.lightness() < 128;
    p.fillRect(event->rect(), dark ? base.lighter(125) : base.darker(104));
    QColor num = pal.color(QPalette::Text);
    num.setAlphaF(0.45f);
    p.setPen(num);
    p.setFont(font());

    QTextBlock block = firstVisibleBlock();
    int top = qRound(blockBoundingGeometry(block).translated(contentOffset()).top());
    int bottom = top + qRound(blockBoundingRect(block).height());
    const int w = m_gutter->width() - GutterPad;
    while (block.isValid() && top <= event->rect().bottom()) {
        const int n = block.blockNumber() + 1;
        const bool marked = n >= m_first && n <= m_last && m_first > 0;
        if (marked) {
            QColor bg = pal.color(QPalette::Highlight);
            bg.setAlphaF(0.35f);
            p.fillRect(0, top, m_gutter->width(), bottom - top, bg);
        }
        p.setPen(marked ? pal.color(QPalette::Text) : num);
        if (block.isVisible() && bottom >= event->rect().top())
            p.drawText(0, top, w, fontMetrics().height(), Qt::AlignRight, QString::number(block.blockNumber() + 1));
        block = block.next();
        top = bottom;
        bottom = top + qRound(blockBoundingRect(block).height());
    }
}
