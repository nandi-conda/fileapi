#pragma once

#include <QProxyStyle>

class QAbstractScrollArea;

// The app's style: Fusion, plus
//  - single-click activation for item views that ask for it (property "singleClick"), and for all of
//    them on touch screens, where double-tapping is awkward;
//  - on touch screens (a coarse pointer, i.e. phones and tablets), finger-sized controls: buttons,
//    fields, list rows and tabs at least 44 px tall (Apple's and Google's minimum touch target).
class AppStyle : public QProxyStyle {
public:
    explicit AppStyle(bool touch);

    bool isTouch() const { return m_touch; }
    // True when the page runs on a touch screen without a mouse (CSS `pointer: coarse`).
    static bool detectTouch();

    int styleHint(StyleHint hint, const QStyleOption *opt = nullptr, const QWidget *w = nullptr,
                  QStyleHintReturn *ret = nullptr) const override;
    int pixelMetric(PixelMetric metric, const QStyleOption *opt = nullptr, const QWidget *w = nullptr) const override;
    QSize sizeFromContents(ContentsType type, const QStyleOption *opt, const QSize &size,
                           const QWidget *w) const override;

private:
    bool m_touch;
};

// The running app's AppStyle (or null before main() sets it).
AppStyle *appStyle();
inline bool isTouchUi()
{
    const AppStyle *s = appStyle();
    return s && s->isTouch();
}

// Finger scrolling: on a touch screen, dragging a list or the code scrolls it (with momentum) instead of
// selecting. A mouse keeps its usual behaviour. Does nothing without a touch screen.
void touchScroll(QAbstractScrollArea *area);
