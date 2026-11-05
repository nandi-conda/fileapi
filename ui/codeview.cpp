#include "codeview.h"

#include <QPainter>
#include <QTextBlock>

namespace {
class Gutter : public QWidget {
public:
    explicit Gutter(CodeView *view) : QWidget(view), m_view(view) {}
    QSize sizeHint() const override { return {m_view->gutterWidth(), 0}; }

protected:
    void paintEvent(QPaintEvent *event) override { m_view->paintGutter(event); }

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
        if (block.isVisible() && bottom >= event->rect().top())
            p.drawText(0, top, w, fontMetrics().height(), Qt::AlignRight, QString::number(block.blockNumber() + 1));
        block = block.next();
        top = bottom;
        bottom = top + qRound(blockBoundingRect(block).height());
    }
}
