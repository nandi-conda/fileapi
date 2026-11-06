#include "appstyle.h"

#include <QAbstractItemView>
#include <QApplication>
#include <QScroller>
#include <QProcessEnvironment>
#include <QStyleFactory>
#include <QWidget>

#ifdef Q_OS_WASM
#include <emscripten/val.h>
#endif

namespace {
constexpr int TouchTarget = 44;  // px
constexpr int TouchRow = 40;     // list rows: a little denser, they sit edge to edge
}  // namespace

AppStyle::AppStyle(bool touch) : QProxyStyle(QStyleFactory::create("Fusion")), m_touch(touch) {}

bool AppStyle::detectTouch()
{
#ifdef Q_OS_WASM
    using emscripten::val;
    val mm = val::global("matchMedia");
    if (mm.typeOf().as<std::string>() != "function")
        return false;
    // Any coarse pointer and no fine one: a phone or tablet (a touch laptop still has its touchpad).
    const bool coarse = val::global().call<val>("matchMedia", std::string("(pointer: coarse)"))["matches"].as<bool>();
    const bool fine = val::global().call<val>("matchMedia", std::string("(any-pointer: fine)"))["matches"].as<bool>();
    return coarse && !fine;
#else
    return QProcessEnvironment::systemEnvironment().value("FORGE_TOUCH") == "1";
#endif
}

int AppStyle::styleHint(StyleHint hint, const QStyleOption *opt, const QWidget *w, QStyleHintReturn *ret) const
{
    if (hint == SH_ItemView_ActivateItemOnSingleClick && (m_touch || (w && w->property("singleClick").toBool())))
        return 1;
    if (m_touch && hint == SH_ToolTip_WakeUpDelay)
        return 1 << 30;  // no hovering on a touch screen: tooltips would only pop up after taps
    return QProxyStyle::styleHint(hint, opt, w, ret);
}

int AppStyle::pixelMetric(PixelMetric metric, const QStyleOption *opt, const QWidget *w) const
{
    if (m_touch) {
        switch (metric) {
        case PM_IndicatorWidth:
        case PM_IndicatorHeight:
        case PM_ExclusiveIndicatorWidth:
        case PM_ExclusiveIndicatorHeight:
            return 22;
        case PM_SmallIconSize:
        case PM_ButtonIconSize:
        case PM_ToolBarIconSize:
            return 20;
        case PM_SplitterWidth:
            return 12;  // something a finger can drag
        default:
            break;
        }
    }
    return QProxyStyle::pixelMetric(metric, opt, w);
}

QSize AppStyle::sizeFromContents(ContentsType type, const QStyleOption *opt, const QSize &size, const QWidget *w) const
{
    QSize s = QProxyStyle::sizeFromContents(type, opt, size, w);
    if (!m_touch)
        return s;
    switch (type) {
    case CT_PushButton:
    case CT_ToolButton:
    case CT_ComboBox:
    case CT_LineEdit:
    case CT_TabBarTab:
        s.setHeight(std::max(s.height(), TouchTarget));
        if (type == CT_ToolButton)
            s.setWidth(std::max(s.width(), TouchTarget));
        break;
    case CT_ItemViewItem:
        s.setHeight(std::max(s.height() + 12, TouchRow));  // two-line rows get breathing room too
        break;
    case CT_CheckBox:
        s.setHeight(std::max(s.height(), TouchRow));
        break;
    default:
        break;
    }
    return s;
}

AppStyle *appStyle() { return dynamic_cast<AppStyle *>(QApplication::style()); }

// Finger scrolling: on a touch screen, dragging a list or the code scrolls it (with momentum) instead of
// selecting. A mouse keeps its usual behaviour.
void touchScroll(QAbstractScrollArea *area)
{
    if (!isTouchUi())
        return;
    if (auto *view = qobject_cast<QAbstractItemView *>(area)) {
        view->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
        view->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
    }
    QScroller::grabGesture(area->viewport(), QScroller::TouchGesture);
    QScrollerProperties props = QScroller::scroller(area->viewport())->scrollerProperties();
    props.setScrollMetric(QScrollerProperties::OvershootDragResistanceFactor, 0.3);
    props.setScrollMetric(QScrollerProperties::OvershootScrollDistanceFactor, 0.1);
    props.setScrollMetric(QScrollerProperties::HorizontalOvershootPolicy, QScrollerProperties::OvershootAlwaysOff);
    QScroller::scroller(area->viewport())->setScrollerProperties(props);
}
