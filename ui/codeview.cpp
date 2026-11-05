#include "codeview.h"

#include <QMouseEvent>
#include <QPainter>
#include <QScrollBar>
#include <QTextBlock>

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
}  // namespace

CodeView::CodeView(QWidget *parent) : QPlainTextEdit(parent), m_gutter(new Gutter(this))
{
    connect(this, &QPlainTextEdit::blockCountChanged, this, [this] { updateGutterWidth(); });
    connect(this, &QPlainTextEdit::updateRequest, this, [this](const QRect &r, int dy) { updateGutter(r, dy); });
    updateGutterWidth();
    m_gutter->setCursor(Qt::PointingHandCursor);
    m_gutter->setToolTip(QStringLiteral("Click a line number to link to it; shift-click to link a range"));
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
            const int lineH = fontMetrics().lineSpacing();
            const int above = std::max(0, viewport()->height() / 3 / std::max(1, lineH));
            verticalScrollBar()->setValue(std::max(0, m_first - 1 - above));
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
