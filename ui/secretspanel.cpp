#include "secretspanel.h"
#include "api.h"

#include <QBrush>
#include <QDateTime>
#include <QFontDatabase>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QLocale>
#include <QPalette>
#include <QPushButton>
#include <QStyle>
#include <QTreeWidget>
#include <QUrl>
#include <QVBoxLayout>

namespace {
constexpr int NameRole = Qt::UserRole;  // set only on the collection's own secrets (inherited rows can't be deleted)

QString when(const QString &iso)
{
    return QLocale().toString(QDateTime::fromString(iso, Qt::ISODate).toLocalTime(), QLocale::ShortFormat);
}
}  // namespace

SecretsPanel::SecretsPanel(Api *api, const QString &title, const QString &note, QWidget *parent)
    : QGroupBox(title, parent), m_api(api)
{
    auto *sl = new QVBoxLayout(this);
    m_note = new QLabel(note);
    m_note->setWordWrap(true);
    m_note->setTextFormat(Qt::RichText);
    sl->addWidget(m_note);
    m_list = new QTreeWidget;
    m_list->setHeaderLabels({"Name", "Updated", ""});
    m_list->setRootIsDecorated(false);
    m_list->setUniformRowHeights(true);
    m_list->setMinimumHeight(90);
    m_list->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_list->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    m_list->header()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    sl->addWidget(m_list);
    auto *addRow = new QHBoxLayout;
    m_name = new QLineEdit;
    m_name->setPlaceholderText("NAME, e.g. FLY_API_TOKEN");
    addRow->addWidget(m_name, 1);
    m_value = new QLineEdit;
    m_value->setPlaceholderText("value");
    m_value->setEchoMode(QLineEdit::Password);
    addRow->addWidget(m_value, 2);
    m_save = new QPushButton("Save secret");
    m_save->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    addRow->addWidget(m_save);
    m_delete = new QPushButton("Delete");
    m_delete->setIcon(style()->standardIcon(QStyle::SP_TrashIcon));
    m_delete->setEnabled(false);
    m_delete->setToolTip("Delete the selected secret");
    addRow->addWidget(m_delete);
    sl->addLayout(addRow);

    connect(m_save, &QPushButton::clicked, this, &SecretsPanel::save);
    connect(m_value, &QLineEdit::returnPressed, this, &SecretsPanel::save);
    connect(m_delete, &QPushButton::clicked, this, &SecretsPanel::remove);
    connect(m_list, &QTreeWidget::itemSelectionChanged, this, [this] {
        const auto sel = m_list->selectedItems();
        const QString name = sel.isEmpty() ? QString() : sel.first()->data(0, NameRole).toString();
        m_delete->setEnabled(!name.isEmpty());
        if (!sel.isEmpty() && !sel.first()->data(0, Qt::UserRole + 1).toString().isEmpty())
            m_name->setText(sel.first()->data(0, Qt::UserRole + 1).toString());  // to replace (or override) it
    });
}

void SecretsPanel::setPath(const QString &path)
{
    m_path = path;
    clear();
}

void SecretsPanel::clear()
{
    m_list->clear();
    m_name->clear();
    m_value->clear();
    m_delete->setEnabled(false);
}

void SecretsPanel::load()
{
    const QString path = m_path;
    if (path.isEmpty())
        return;
    m_api->get(path, {}, this, [this, path](const QJsonObject &body, const QString &error) {
        if (path != m_path)
            return;
        m_list->clear();
        m_delete->setEnabled(false);
        if (!error.isEmpty()) {
            (new QTreeWidgetItem(m_list))->setText(0, "Couldn't load secrets: " + error);
            return;
        }
        const bool enabled = body.value("enabled").toBool();
        m_name->setEnabled(enabled);
        m_value->setEnabled(enabled);
        m_save->setEnabled(enabled);
        if (!enabled) {
            (new QTreeWidgetItem(m_list))->setText(0, "Secrets are off: the server needs "
                                                      + body.value("key_env").toString() + " set.");
            return;
        }
        const QJsonArray own = body.value("secrets").toArray();
        const QJsonArray inherited = body.value("inherited").toArray();
        if (own.isEmpty() && inherited.isEmpty())
            (new QTreeWidgetItem(m_list))->setText(0, "No secrets yet");
        const QFont mono = QFontDatabase::systemFont(QFontDatabase::FixedFont);
        for (const auto &v : own) {
            const QJsonObject s = v.toObject();
            auto *it = new QTreeWidgetItem(m_list);
            it->setText(0, s.value("name").toString());
            it->setFont(0, mono);
            it->setData(0, NameRole, s.value("name").toString());
            it->setData(0, Qt::UserRole + 1, s.value("name").toString());
            it->setText(1, when(s.value("updated").toString()));
        }
        // A repo's view of its owner's user secrets: shown, not deletable here; selecting one fills the name
        // in, so saving makes a repo secret that overrides it.
        const QBrush dim = palette().brush(QPalette::Disabled, QPalette::Text);
        for (const auto &v : inherited) {
            const QJsonObject s = v.toObject();
            const bool over = s.value("overridden").toBool();
            auto *it = new QTreeWidgetItem(m_list);
            it->setText(0, s.value("name").toString());
            it->setFont(0, mono);
            it->setData(0, Qt::UserRole + 1, s.value("name").toString());
            it->setText(1, when(s.value("updated").toString()));
            it->setText(2, over ? "user secret, overridden" : "user secret");
            it->setToolTip(0, over ? "Your user secret of this name is overridden by this repo's own."
                                   : "From your user settings: every repo you own gets it. Save a value "
                                     "here to override it for this repo.");
            for (int c = 0; c < 3; ++c)
                it->setForeground(c, dim);
            if (over) {
                QFont f = mono;
                f.setStrikeOut(true);
                it->setFont(0, f);
            }
        }
    });
}

void SecretsPanel::save()
{
    const QString path = m_path;
    const QString name = m_name->text().trimmed();
    if (path.isEmpty())
        return;
    if (name.isEmpty())
        return emit statusText("Type a name for the secret, e.g. FLY_API_TOKEN.", false);
    if (m_value->text().isEmpty())
        return emit statusText("Type the secret's value.", false);
    QJsonObject body;
    body["value"] = m_value->text();
    m_save->setEnabled(false);
    emit statusText("Saving secret " + name + "…", false);
    m_api->send("PUT", path + "/" + QString::fromLatin1(QUrl::toPercentEncoding(name)), body, this,
                [this, name](const QJsonObject &, const QString &error) {
        m_save->setEnabled(true);
        if (!error.isEmpty())
            return emit statusText("Could not save " + name + ": " + error, true);
        m_value->clear();  // never kept around in the page
        m_name->clear();
        emit statusText("Saved secret " + name + ".", false);
        load();
    });
}

void SecretsPanel::remove()
{
    const auto sel = m_list->selectedItems();
    if (m_path.isEmpty() || sel.isEmpty())
        return;
    const QString name = sel.first()->data(0, NameRole).toString();
    if (name.isEmpty())
        return;
    m_delete->setEnabled(false);
    emit statusText("Deleting secret " + name + "…", false);
    m_api->send("DELETE", m_path + "/" + QString::fromLatin1(QUrl::toPercentEncoding(name)), {}, this,
                [this, name](const QJsonObject &, const QString &error) {
        if (!error.isEmpty())
            return emit statusText("Could not delete " + name + ": " + error, true);
        m_name->clear();
        emit statusText("Deleted secret " + name + ".", false);
        load();
    });
}
