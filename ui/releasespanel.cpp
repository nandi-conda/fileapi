#include "releasespanel.h"
#include "api.h"
#include "appstyle.h"

#include <QApplication>
#include <QCheckBox>
#include <QClipboard>
#include <QDateTime>
#include <QDesktopServices>
#include <QFileDialog>
#include <QFileInfo>
#include <QFontDatabase>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QLocale>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSplitter>
#include <QStackedWidget>
#include <QStyle>
#include <QTextBrowser>
#include <QTimer>
#include <QTreeWidget>
#include <QUrl>
#include <QUrlQuery>
#include <QVBoxLayout>

namespace {
constexpr int TagRole = Qt::UserRole;
constexpr qint64 Piece = 8 * 1024 * 1024;  // per request: well under hosting proxies' body limits

QString when(const QString &iso)
{
    return QLocale().toString(QDateTime::fromString(iso, Qt::ISODate).toLocalTime(), QLocale::ShortFormat);
}

QString humanSize(qint64 n)
{
    return QLocale::c().formattedDataSize(n, 1, QLocale::DataSizeTraditionalFormat);
}

QString enc(const QString &s) { return QString::fromLatin1(QUrl::toPercentEncoding(s)); }
}  // namespace

ReleasesPanel::ReleasesPanel(Api *api, QWidget *parent) : QWidget(parent), m_api(api)
{
    auto *lay = new QVBoxLayout(this);
    lay->setContentsMargins(8, 8, 8, 8);
    auto *top = new QHBoxLayout;
    m_note = new QLabel;
    m_note->setWordWrap(true);
    m_note->setTextFormat(Qt::RichText);
    m_note->setTextInteractionFlags(Qt::TextSelectableByMouse);
    top->addWidget(m_note, 1);
    m_new = new QPushButton("New release");
    m_new->setIcon(style()->standardIcon(QStyle::SP_FileDialogNewFolder));
    m_new->setVisible(false);
    top->addWidget(m_new);
    m_refresh = new QPushButton("Refresh");
    m_refresh->setToolTip("Refresh");
    m_refresh->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    top->addWidget(m_refresh);
    lay->addLayout(top);

    m_split = new QSplitter;
    m_list = new QTreeWidget;
    m_list->setHeaderLabels({"Release", "Date", "Files"});
    m_list->setRootIsDecorated(false);
    m_list->setUniformRowHeights(true);
    m_list->header()->setStretchLastSection(false);
    m_list->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_list->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    m_list->header()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    m_list->setProperty("singleClick", true);
    touchScroll(m_list);
    m_split->addWidget(m_list);

    m_right = new QStackedWidget;
    // -- the selected release
    auto *view = new QWidget;
    auto *vl = new QVBoxLayout(view);
    vl->setContentsMargins(8, 0, 0, 0);
    m_title = new QLabel;
    QFont tf = font();
    tf.setPointSizeF(tf.pointSizeF() * 1.3);
    tf.setBold(true);
    m_title->setFont(tf);
    m_title->setWordWrap(true);
    vl->addWidget(m_title);
    m_meta = new QLabel;
    m_meta->setWordWrap(true);
    m_meta->setTextInteractionFlags(Qt::TextSelectableByMouse);
    vl->addWidget(m_meta);
    m_notes = new QTextBrowser;
    m_notes->setOpenExternalLinks(true);
    m_notes->setMinimumHeight(80);
    touchScroll(m_notes);
    vl->addWidget(m_notes, 2);
    m_assets = new QTreeWidget;
    m_assets->setHeaderLabels({"File", "Size", "Downloads"});
    m_assets->setRootIsDecorated(false);
    m_assets->setUniformRowHeights(true);
    m_assets->header()->setStretchLastSection(false);
    m_assets->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_assets->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    m_assets->header()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    m_assets->setToolTip("Double-click (or tap) a file to download it.");
    touchScroll(m_assets);
    vl->addWidget(m_assets, 1);
    m_assetButtons = new QBoxLayout(QBoxLayout::LeftToRight);
    m_download = new QPushButton("Download");
    m_download->setIcon(style()->standardIcon(QStyle::SP_ArrowDown));
    m_download->setEnabled(false);
    m_assetButtons->addWidget(m_download);
    m_copyAsset = new QPushButton("Copy file link");
    m_copyAsset->setIcon(style()->standardIcon(QStyle::SP_FileLinkIcon));
    m_copyAsset->setToolTip("Copy this file's download URL");
    m_copyAsset->setEnabled(false);
    m_assetButtons->addWidget(m_copyAsset);
    m_upload = new QPushButton("Upload file…");
    m_upload->setIcon(style()->standardIcon(QStyle::SP_ArrowUp));
    m_assetButtons->addWidget(m_upload);
    m_deleteAsset = new QPushButton("Delete file");
    m_deleteAsset->setIcon(style()->standardIcon(QStyle::SP_TrashIcon));
    m_deleteAsset->setEnabled(false);
    m_assetButtons->addWidget(m_deleteAsset);
    m_assetButtons->addStretch(1);
    vl->addLayout(m_assetButtons);
    m_releaseButtons = new QBoxLayout(QBoxLayout::LeftToRight);
    m_copyRelease = new QPushButton("Copy release link");
    m_copyRelease->setIcon(style()->standardIcon(QStyle::SP_FileLinkIcon));
    m_copyRelease->setToolTip("Copy a link to this release's page");
    m_releaseButtons->addWidget(m_copyRelease);
    m_edit = new QPushButton("Edit");
    m_edit->setIcon(style()->standardIcon(QStyle::SP_FileDialogContentsView));
    m_releaseButtons->addWidget(m_edit);
    m_publish = new QPushButton("Publish");
    m_publish->setIcon(style()->standardIcon(QStyle::SP_DialogApplyButton));
    m_publish->setToolTip("Make this draft visible to everyone who can see the repo");
    m_releaseButtons->addWidget(m_publish);
    m_deleteRelease = new QPushButton("Delete release");
    m_deleteRelease->setIcon(style()->standardIcon(QStyle::SP_TrashIcon));
    m_releaseButtons->addWidget(m_deleteRelease);
    m_releaseButtons->addStretch(1);
    vl->addLayout(m_releaseButtons);
    m_right->addWidget(view);

    // -- new / edit
    auto *editor = new QWidget;
    auto *el = new QVBoxLayout(editor);
    el->setContentsMargins(8, 0, 0, 0);
    m_editHeading = new QLabel;
    m_editHeading->setFont(tf);
    el->addWidget(m_editHeading);
    auto *form = new QFormLayout;
    form->setRowWrapPolicy(QFormLayout::WrapLongRows);
    m_editTag = new QLineEdit;
    m_editTag->setPlaceholderText("v1.0.0");
    m_editTag->setToolTip("Letters, digits and . _ + -. If the tag exists in git, the release points at its commit; "
                          "otherwise at the default branch's latest commit.");
    form->addRow("Tag:", m_editTag);
    m_editName = new QLineEdit;
    m_editName->setPlaceholderText("Title (default: the tag)");
    form->addRow("Title:", m_editName);
    el->addLayout(form);
    m_editNotes = new QPlainTextEdit;
    m_editNotes->setPlaceholderText("Release notes (Markdown). Leave empty and a changelog of the commits since the previous release is written for you.");
    el->addWidget(m_editNotes, 1);
    auto *flags = new QHBoxLayout;
    m_editPre = new QCheckBox("Prerelease");
    m_editPre->setToolTip("Not offered as the latest release");
    flags->addWidget(m_editPre);
    m_editDraft = new QCheckBox("Draft");
    m_editDraft->setToolTip("Only you can see it until you publish it");
    flags->addWidget(m_editDraft);
    flags->addStretch(1);
    el->addLayout(flags);
    auto *eb = new QHBoxLayout;
    m_editSave = new QPushButton("Save");
    m_editSave->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    eb->addWidget(m_editSave);
    auto *cancel = new QPushButton("Cancel");
    cancel->setIcon(style()->standardIcon(QStyle::SP_DialogCancelButton));
    eb->addWidget(cancel);
    eb->addStretch(1);
    el->addLayout(eb);
    m_right->addWidget(editor);
    m_split->addWidget(m_right);
    m_split->setStretchFactor(0, 2);
    m_split->setStretchFactor(1, 3);
    lay->addWidget(m_split, 1);

    connect(m_refresh, &QPushButton::clicked, this, &ReleasesPanel::load);
    connect(m_new, &QPushButton::clicked, this, [this] { openEditor({}); });
    connect(m_list, &QTreeWidget::itemSelectionChanged, this, [this] {
        const auto sel = m_list->selectedItems();
        if (sel.isEmpty())
            return;
        const QString tag = sel.first()->data(0, TagRole).toString();
        for (const auto &v : m_releases)
            if (v.toObject().value("tag").toString() == tag)
                showRelease(v.toObject());
        m_picked = tag;
        emit tagPicked(tag);
    });
    connect(m_assets, &QTreeWidget::itemSelectionChanged, this, [this] {
        const bool any = !m_assets->selectedItems().isEmpty()
                         && !m_assets->selectedItems().first()->data(0, TagRole).toString().isEmpty();
        m_download->setEnabled(any);
        m_copyAsset->setEnabled(any);
        m_deleteAsset->setEnabled(any && m_canEdit);
    });
    connect(m_assets, &QTreeWidget::itemActivated, this, [this] { downloadAsset(); });
    connect(m_download, &QPushButton::clicked, this, &ReleasesPanel::downloadAsset);
    connect(m_copyAsset, &QPushButton::clicked, this, [this] {
        const auto sel = m_assets->selectedItems();
        const QString url = sel.isEmpty() ? QString() : sel.first()->data(0, TagRole + 1).toString();
        if (url.isEmpty())
            return emit statusText("That file is still uploading.", false);
        copyText(url, url.contains("?dl=") ? "download link (private repo: it works for an hour)" : "download link");
    });
    connect(m_copyRelease, &QPushButton::clicked, this, [this] {
        copyText(m_api->baseUrl() + "/forge/" + m_repo + "/-/releases/" + QString::fromLatin1(QUrl::toPercentEncoding(m_shownTag)),
                 "link to " + m_shownTag);
    });
    connect(m_upload, &QPushButton::clicked, this, &ReleasesPanel::upload);
    connect(m_deleteAsset, &QPushButton::clicked, this, &ReleasesPanel::deleteAsset);
    connect(m_edit, &QPushButton::clicked, this, [this] { openEditor(current()); });
    connect(m_publish, &QPushButton::clicked, this, &ReleasesPanel::publish);
    connect(m_deleteRelease, &QPushButton::clicked, this, &ReleasesPanel::deleteRelease);
    connect(m_editSave, &QPushButton::clicked, this, &ReleasesPanel::saveEditor);
    connect(cancel, &QPushButton::clicked, this, [this] {
        m_right->setCurrentIndex(0);
        if (m_releases.isEmpty())
            m_right->setVisible(false);
    });
    setRepo(QString());
}

void ReleasesPanel::setCompact(bool compact)
{
    m_split->setOrientation(compact ? Qt::Vertical : Qt::Horizontal);
    m_list->setColumnHidden(2, compact);
    m_assets->setColumnHidden(2, compact);
    m_assetButtons->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
    m_releaseButtons->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
    m_new->setText(compact ? "New" : "New release");
    m_refresh->setText(compact ? QString() : "Refresh");
    m_note->setVisible(!compact || m_releases.isEmpty());
    m_split->setSizes(compact ? QList<int>{1, 2} : QList<int>{2, 3});  // proportions; the splitter scales them up
    m_list->setMinimumWidth(compact ? 0 : 300);
    m_list->setMinimumHeight(compact ? m_list->fontMetrics().lineSpacing() * 8 : 0);  // a few rows on a phone
}

void ReleasesPanel::setRepo(const QString &repo)
{
    m_repo = repo;
    m_loaded = false;
    m_releases = {};
    m_shownTag.clear();
    m_picked.clear();
    m_list->clear();
    m_assets->clear();
    m_right->setCurrentIndex(0);
    m_right->setVisible(false);
    m_new->setVisible(false);
    m_note->setText(QString());
}

void ReleasesPanel::selectTag(const QString &tag)
{
    m_picked = tag;
    m_shownTag = tag;
    if (m_loaded && m_right->currentIndex() == 0)
        fillList(tag);
}

QJsonObject ReleasesPanel::current() const
{
    for (const auto &v : m_releases)
        if (v.toObject().value("tag").toString() == m_shownTag)
            return v.toObject();
    return {};
}

void ReleasesPanel::load()
{
    const QString repo = m_repo;
    if (repo.isEmpty())
        return;
    m_api->get("repos/" + repo + "/releases", {}, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty()) {
            m_note->setText("Couldn't load releases: " + error.toHtmlEscaped());
            return emit statusText("Couldn't load releases: " + error, true);
        }
        m_loaded = true;
        m_releases = body.value("releases").toArray();
        m_canEdit = body.value("can_edit").toBool();
        m_maxAsset = body.value("max_asset").toInteger();
        m_new->setVisible(m_canEdit);
        if (m_releases.isEmpty())
            m_note->setText(m_canEdit
                ? "No releases yet. Make one with <b>New release</b>, or publish from a deploy job: a job with "
                  "<code>tags = [\"v*\"]</code> in <code>.forge/jobs.toml</code> runs when you push a tag and can run "
                  "<code>forge-release upload --create $FORGE_TAG dist/*</code>."
                : "No releases yet.");
        else
            m_note->setText(QStringLiteral("%1 release%2").arg(m_releases.size()).arg(m_releases.size() == 1 ? "" : "s"));
        emit countChanged(m_releases.size());
        fillList(m_shownTag);
    });
}

void ReleasesPanel::fillList(const QString &select)
{
    const QSignalBlocker block(m_list);
    m_list->clear();
    QTreeWidgetItem *pick = nullptr;
    bool latestMarked = false;
    for (const auto &v : m_releases) {
        const QJsonObject r = v.toObject();
        auto *it = new QTreeWidgetItem(m_list);
        const QString tag = r.value("tag").toString();
        QString label = r.value("name").toString();
        if (label != tag)
            label += "  (" + tag + ")";
        QStringList flags;
        if (r.value("draft").toBool())
            flags << "draft";
        if (r.value("prerelease").toBool())
            flags << "prerelease";
        if (flags.isEmpty() && !latestMarked) {
            flags << "latest";
            latestMarked = true;
        }
        it->setText(0, label + (flags.isEmpty() ? QString() : "  · " + flags.join(", ")));
        it->setIcon(0, style()->standardIcon(r.value("draft").toBool() ? QStyle::SP_FileDialogDetailedView
                                                                       : QStyle::SP_DriveFDIcon));
        it->setText(1, QLocale().toString(QDateTime::fromString(r.value("created").toString(), Qt::ISODate)
                                              .toLocalTime().date(), QLocale::ShortFormat));
        it->setToolTip(0, label);
        it->setText(2, QString::number(r.value("assets").toArray().size()));
        it->setData(0, TagRole, tag);
        if (tag == select || (!pick && select.isEmpty()))
            pick = it;
    }
    if (!pick && m_list->topLevelItemCount() > 0)
        pick = m_list->topLevelItem(0);
    if (m_right->currentIndex() == 1)  // the editor is open: leave it be
        return;
    if (pick) {
        pick->setSelected(true);
        m_list->setCurrentItem(pick);
        for (const auto &v : m_releases)
            if (v.toObject().value("tag").toString() == pick->data(0, TagRole).toString())
                showRelease(v.toObject());
    } else {
        m_shownTag.clear();
        m_right->setVisible(false);
    }
}

void ReleasesPanel::showRelease(const QJsonObject &rel)
{
    m_shownTag = rel.value("tag").toString();
    m_right->setVisible(true);
    m_right->setCurrentIndex(0);
    m_title->setText(rel.value("name").toString());
    QStringList meta;
    meta << "Tag " + m_shownTag + (rel.value("tagged").toBool() ? QString() : " (not a git tag)");
    const QString target = rel.value("target").toString();
    if (!target.isEmpty())
        meta << "commit " + target.left(10);
    meta << when(rel.value("created").toString());
    if (rel.value("draft").toBool())
        meta << "draft: only you can see it";
    if (rel.value("prerelease").toBool())
        meta << "prerelease";
    m_meta->setText(meta.join("  ·  "));
    const QString notes = rel.value("notes").toString();
#if QT_CONFIG(textmarkdownreader)
    m_notes->setMarkdown(notes.isEmpty() ? QStringLiteral("*No release notes.*") : notes);
#else
    m_notes->setPlainText(notes.isEmpty() ? QStringLiteral("No release notes.") : notes);
#endif
    m_assets->clear();
    const QJsonArray assets = rel.value("assets").toArray();
    if (assets.isEmpty())
        (new QTreeWidgetItem(m_assets))->setText(0, m_canEdit ? "No files yet: Upload file… adds one." : "No files.");
    for (const auto &v : assets) {
        const QJsonObject a = v.toObject();
        auto *it = new QTreeWidgetItem(m_assets);
        const bool ready = a.value("state").toString() == "ready";
        it->setText(0, a.value("name").toString() + (ready ? QString() : "  (uploading)"));
        it->setIcon(0, style()->standardIcon(QStyle::SP_FileIcon));
        it->setText(1, humanSize(a.value("size").toInteger()));
        it->setText(2, QString::number(a.value("downloads").toInteger()));
        it->setToolTip(0, (ready ? a.value("download_url").toString().section('?', 0, 0) + "\n" : QString())
                              + "sha256 " + a.value("sha256").toString());
        it->setData(0, TagRole, a.value("name").toString());
        it->setData(0, TagRole + 1, ready ? a.value("download_url").toString() : QString());
    }
    m_download->setEnabled(false);
    m_copyAsset->setEnabled(false);
    m_deleteAsset->setEnabled(false);
    for (QPushButton *b : {m_upload, m_edit, m_deleteRelease})
        b->setVisible(m_canEdit);
    m_deleteAsset->setVisible(m_canEdit);
    m_publish->setVisible(m_canEdit && rel.value("draft").toBool());
    m_upload->setEnabled(!m_uploading);
}

void ReleasesPanel::openEditor(const QJsonObject &rel)
{
    m_editing = rel.value("tag").toString();
    m_editHeading->setText(m_editing.isEmpty() ? "New release" : "Edit " + m_editing);
    m_editTag->setText(m_editing);
    m_editName->setText(rel.isEmpty() || rel.value("name").toString() == m_editing ? QString()
                                                                                    : rel.value("name").toString());
    m_editNotes->setPlainText(rel.value("notes").toString());
    m_editPre->setChecked(rel.value("prerelease").toBool());
    m_editDraft->setChecked(rel.value("draft").toBool());
    m_editSave->setText(m_editing.isEmpty() ? "Create release" : "Save");
    m_right->setVisible(true);
    m_right->setCurrentIndex(1);
    m_editTag->setFocus();
}

void ReleasesPanel::saveEditor()
{
    const QString tag = m_editTag->text().trimmed();
    if (tag.isEmpty())
        return emit statusText("Give the release a tag, e.g. v1.0.0.", false);
    QJsonObject body;
    body["tag"] = tag;
    body["name"] = m_editName->text().trimmed();
    body["notes"] = m_editNotes->toPlainText();
    body["prerelease"] = m_editPre->isChecked();
    body["draft"] = m_editDraft->isChecked();
    const bool isNew = m_editing.isEmpty();
    const QString path = "repos/" + m_repo + "/releases" + (isNew ? QString() : "/" + enc(m_editing));
    m_editSave->setEnabled(false);
    emit statusText((isNew ? "Creating release " : "Saving release ") + tag + "…", false);
    m_api->send(isNew ? "POST" : "PATCH", path, body, this, [this, tag, isNew](const QJsonObject &, const QString &error) {
        m_editSave->setEnabled(true);
        if (!error.isEmpty())
            return emit statusText("Could not save " + tag + ": " + error, true);
        emit statusText((isNew ? "Created release " : "Saved release ") + tag + ".", false);
        m_right->setCurrentIndex(0);
        m_shownTag = tag;
        load();
    });
}

void ReleasesPanel::upload()
{
    const QString tag = m_shownTag;
    if (tag.isEmpty() || m_uploading)
        return;
    // In the browser this opens the file picker and hands over the file's bytes (it reads it into memory).
    QFileDialog::getOpenFileContent(QString(), [this, tag](const QString &fileName, const QByteArray &data) {
        if (fileName.isEmpty())
            return;  // cancelled
        const QString name = QFileInfo(fileName).fileName();
        if (m_maxAsset > 0 && data.size() > m_maxAsset)
            return emit statusText(QStringLiteral("%1 is %2; the server takes files up to %3.")
                                       .arg(name, humanSize(data.size()), humanSize(m_maxAsset)), true);
        m_uploading = true;
        m_upload->setEnabled(false);
        sendPiece(tag, name, data, 0);
    }, this);
}

void ReleasesPanel::sendPiece(const QString &tag, const QString &name, const QByteArray &data, qint64 offset)
{
    const qint64 n = std::min<qint64>(Piece, data.size() - offset);
    const bool final = offset + n >= data.size();
    QUrlQuery q;
    q.addQueryItem("offset", QString::number(offset));
    q.addQueryItem("final", final ? "true" : "false");
    if (data.size() > Piece)
        emit statusText(QStringLiteral("Uploading %1… %2%").arg(name).arg(offset * 100 / data.size()), false);
    else
        emit statusText("Uploading " + name + "…", false);
    const QString repo = m_repo;
    m_api->put("repos/" + repo + "/releases/" + enc(tag) + "/assets/" + enc(name), q, data.mid(offset, n),
               "application/octet-stream", this,
               [this, repo, tag, name, data, offset, n, final](const QJsonObject &, const QString &error) {
        if (!error.isEmpty() || !final) {
            if (error.isEmpty() && repo == m_repo)
                return sendPiece(tag, name, data, offset + n);
        }
        m_uploading = false;
        m_upload->setEnabled(true);
        if (!error.isEmpty())
            return emit statusText("Could not upload " + name + ": " + error, true);
        emit statusText(QStringLiteral("Uploaded %1 (%2) to %3.").arg(name, humanSize(data.size()), tag), false);
        if (repo == m_repo)
            load();
    });
}

void ReleasesPanel::copyText(const QString &text, const QString &what)
{
    QApplication::clipboard()->setText(text);
    emit statusText("Copied " + what + ": " + text, false);
}

void ReleasesPanel::downloadAsset()
{
    const auto sel = m_assets->selectedItems();
    if (sel.isEmpty())
        return;
    const QString url = sel.first()->data(0, TagRole + 1).toString();
    if (url.isEmpty())
        return emit statusText("That file is still uploading.", false);
    QDesktopServices::openUrl(QUrl(url));  // the server sends it as an attachment, so the browser saves it
    emit statusText("Downloading " + sel.first()->data(0, TagRole).toString() + "…", false);
}

bool ReleasesPanel::confirm(QPushButton *button, const QString &armedText)
{
    if (button->property("armed").toBool())
        return true;
    const QString text = button->text();
    button->setProperty("armed", true);
    button->setText(armedText);
    QTimer::singleShot(4000, button, [button, text] {
        button->setProperty("armed", false);
        button->setText(text);
    });
    return false;
}

void ReleasesPanel::deleteAsset()
{
    const auto sel = m_assets->selectedItems();
    const QString tag = m_shownTag;
    if (sel.isEmpty() || tag.isEmpty())
        return;
    const QString name = sel.first()->data(0, TagRole).toString();
    if (name.isEmpty() || !confirm(m_deleteAsset, "Click again to delete"))
        return;
    m_deleteAsset->setProperty("armed", false);
    m_deleteAsset->setText("Delete file");
    m_api->send("DELETE", "repos/" + m_repo + "/releases/" + enc(tag) + "/assets/" + enc(name), {}, this,
                [this, name](const QJsonObject &, const QString &error) {
        if (!error.isEmpty())
            return emit statusText("Could not delete " + name + ": " + error, true);
        emit statusText("Deleted " + name + ".", false);
        load();
    });
}

void ReleasesPanel::deleteRelease()
{
    const QString tag = m_shownTag;
    if (tag.isEmpty() || !confirm(m_deleteRelease, "Click again: deletes it and its files"))
        return;
    m_deleteRelease->setProperty("armed", false);
    m_deleteRelease->setText("Delete release");
    m_api->send("DELETE", "repos/" + m_repo + "/releases/" + enc(tag), {}, this,
                [this, tag](const QJsonObject &, const QString &error) {
        if (!error.isEmpty())
            return emit statusText("Could not delete " + tag + ": " + error, true);
        emit statusText("Deleted release " + tag + ".", false);
        m_shownTag.clear();
        load();
    });
}

void ReleasesPanel::publish()
{
    const QString tag = m_shownTag;
    if (tag.isEmpty())
        return;
    QJsonObject body;
    body["draft"] = false;
    m_api->send("PATCH", "repos/" + m_repo + "/releases/" + enc(tag), body, this,
                [this, tag](const QJsonObject &, const QString &error) {
        if (!error.isEmpty())
            return emit statusText("Could not publish " + tag + ": " + error, true);
        emit statusText("Published " + tag + ".", false);
        load();
    });
}
