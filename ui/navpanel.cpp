#include "navpanel.h"

#include <QFontDatabase>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QMap>
#include <QStyle>
#include <QToolButton>
#include <QTreeWidget>
#include <QVBoxLayout>

namespace {
constexpr int PathRole = Qt::UserRole;
constexpr int LineRole = Qt::UserRole + 1;
constexpr int ColRole = Qt::UserRole + 2;
constexpr int EndRole = Qt::UserRole + 3;

QString plural(int n, const char *one, const char *many)
{
    return QStringLiteral("%1 %2").arg(n).arg(QLatin1String(n == 1 ? one : many));
}
}  // namespace

NavPanel::NavPanel(QWidget *parent) : QWidget(parent)
{
    auto *lay = new QVBoxLayout(this);
    lay->setContentsMargins(4, 0, 0, 0);
    auto *head = new QHBoxLayout;
    m_back = new QToolButton;
    m_back->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    m_back->setToolTip("Back to where you were (Alt+Left)");
    m_back->setAutoRaise(true);
    m_back->setEnabled(false);
    head->addWidget(m_back);
    m_title = new QLabel;
    m_title->setTextFormat(Qt::RichText);
    m_title->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    head->addWidget(m_title, 1);
    auto *close = new QToolButton;
    close->setIcon(style()->standardIcon(QStyle::SP_TitleBarCloseButton));
    close->setToolTip("Close");
    close->setAutoRaise(true);
    head->addWidget(close);
    lay->addLayout(head);
    m_sub = new QLabel;
    m_sub->setWordWrap(true);
    m_sub->setTextFormat(Qt::RichText);
    m_sub->setTextInteractionFlags(Qt::TextSelectableByMouse);
    lay->addWidget(m_sub);
    m_tree = new QTreeWidget;
    m_tree->setHeaderHidden(true);
    m_tree->setUniformRowHeights(true);
    m_tree->setIndentation(14);
    m_tree->setCursor(Qt::PointingHandCursor);
    m_tree->header()->setStretchLastSection(true);
    lay->addWidget(m_tree, 1);

    connect(close, &QToolButton::clicked, this, [this] { if (onClose) onClose(); });
    connect(m_back, &QToolButton::clicked, this, [this] { if (onBack) onBack(); });
    connect(m_tree, &QTreeWidget::itemClicked, this, [this](QTreeWidgetItem *it) {
        const QString path = it->data(0, PathRole).toString();
        if (!path.isEmpty() && onOpen)
            onOpen(path, it->data(0, LineRole).toInt(), it->data(0, ColRole).toInt(), it->data(0, EndRole).toInt());
    });
}

void NavPanel::setBackEnabled(bool on) { m_back->setEnabled(on); }

void NavPanel::setLoading(const QString &what)
{
    m_title->setText(QStringLiteral("<b>%1</b>").arg(what.toHtmlEscaped()));
    m_sub->setText("Looking it up…");
    m_tree->clear();
}

void NavPanel::setError(const QString &error)
{
    m_sub->setText(QStringLiteral("<span style=\"color:#c62828\">%1</span>").arg(error.toHtmlEscaped()));
    m_tree->clear();
}

void NavPanel::setResult(const QJsonObject &r)
{
    m_tree->clear();
    const QJsonObject sym = r.value("symbol").toObject();
    const QJsonArray defs = r.value("definitions").toArray();
    const QJsonArray refs = r.value("references").toArray();
    const int refCount = r.value("reference_count").toInt(refs.size());
    const QString name = sym.value("name").toString();
    const QString kind = defs.isEmpty() ? QString() : defs.first().toObject().value("kind").toString();
    m_title->setText(QStringLiteral("<b><code>%1</code></b> %2").arg(name.toHtmlEscaped(), kind.toHtmlEscaped()));

    QString sub = plural(defs.size(), "definition", "definitions") + " · " + plural(refCount, "reference", "references");
    if (refCount > refs.size())
        sub += QStringLiteral(" (first %1 shown)").arg(refs.size());
    if (defs.isEmpty())
        sub += "<br><i>Not defined in this repository (a builtin or a library?).</i>";
    else if (defs.size() > 1)
        sub += "<br><i>Matched by name: pick the right one.</i>";
    m_sub->setText(sub);

    const QFont mono = QFontDatabase::systemFont(QFontDatabase::FixedFont);
    auto row = [&](QTreeWidgetItem *parent, const QJsonObject &o, const QString &text) {
        auto *it = new QTreeWidgetItem(parent);
        it->setText(0, text);
        it->setFont(0, mono);
        it->setData(0, PathRole, o.value("path").toString());
        it->setData(0, LineRole, o.value("line").toInt());
        it->setData(0, ColRole, o.value("col").toInt());
        it->setData(0, EndRole, o.value("end_col").toInt());
        return it;
    };

    auto *defRoot = new QTreeWidgetItem(m_tree, {QStringLiteral("Definitions (%1)").arg(defs.size())});
    QFont bold = defRoot->font(0);
    bold.setBold(true);
    defRoot->setFont(0, bold);
    for (const auto &v : defs) {
        const QJsonObject d = v.toObject();
        auto *it = row(defRoot, d, QStringLiteral("%1:%2  %3").arg(d.value("path").toString())
                                       .arg(d.value("line").toInt()).arg(d.value("signature").toString()));
        QString tip = "<pre>" + d.value("signature").toString().toHtmlEscaped() + "</pre>";
        if (!d.value("doc").toString().isEmpty())
            tip += "<p style=\"white-space:pre-wrap\">" + d.value("doc").toString().toHtmlEscaped() + "</p>";
        it->setToolTip(0, tip);
    }

    auto *refRoot = new QTreeWidgetItem(m_tree, {QStringLiteral("References (%1)").arg(refCount)});
    refRoot->setFont(0, bold);
    // Group by file, keeping the server's order (the current file first).
    QStringList order;
    QMap<QString, QList<QJsonObject>> byFile;
    for (const auto &v : refs) {
        const QJsonObject o = v.toObject();
        const QString p = o.value("path").toString();
        if (!byFile.contains(p))
            order.append(p);
        byFile[p].append(o);
    }
    for (const QString &p : std::as_const(order)) {
        const auto &list = byFile[p];
        auto *file = new QTreeWidgetItem(refRoot, {QStringLiteral("%1 (%2)").arg(p).arg(list.size())});
        file->setIcon(0, style()->standardIcon(QStyle::SP_FileIcon));
        for (const QJsonObject &o : list) {
            auto *it = row(file, o, QStringLiteral("%1  %2").arg(o.value("line").toInt(), 4)
                                        .arg(o.value("text").toString().trimmed()));
            if (o.value("definition").toBool())
                it->setToolTip(0, "Definition");
        }
        file->setExpanded(order.size() <= 12);
    }
    defRoot->setExpanded(true);
    refRoot->setExpanded(true);
}
