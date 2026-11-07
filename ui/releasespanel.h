// The Releases tab: a repo's releases (tag, title, notes, files to download), and for the signed-in owner,
// making, editing and deleting them and uploading files (in pieces, so big files fit under request limits).
#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QWidget>

class Api;
class QBoxLayout;
class QCheckBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QPushButton;
class QSplitter;
class QStackedWidget;
class QTextBrowser;
class QTreeWidget;

class ReleasesPanel : public QWidget {
    Q_OBJECT
public:
    explicit ReleasesPanel(Api *api, QWidget *parent = nullptr);

    // The repo to show ("<owner>/<repo>"); empty clears it. Loads when the tab is shown.
    void setRepo(const QString &repo);
    void load();
    bool loaded() const { return m_loaded; }
    void setCompact(bool compact);
    // The release the page URL names (or the user picked); empty = none picked, the newest shows.
    void selectTag(const QString &tag);
    QString pickedTag() const { return m_picked; }

signals:
    void statusText(const QString &text, bool error);
    void countChanged(int releases);
    void tagPicked(const QString &tag);  // the user selected a release in the list

private:
    void fillList(const QString &select);
    void showRelease(const QJsonObject &rel);
    QJsonObject current() const;
    void openEditor(const QJsonObject &rel);  // empty = a new release
    void saveEditor();
    void generateNotes();  // fill the notes with the commits since the previous release or tag
    void upload();
    void sendPiece(const QString &tag, const QString &name, const QByteArray &data, qint64 offset);
    void downloadAsset();
    void copyText(const QString &text, const QString &what);
    void deleteAsset();
    void deleteRelease();
    void publish();
    // Two clicks to delete: the first arms the button for a few seconds.
    bool confirm(QPushButton *button, const QString &armedText);

    Api *m_api;
    QString m_repo;
    bool m_loaded = false;
    bool m_canEdit = false;
    qint64 m_maxAsset = 0;
    QJsonArray m_releases;
    QString m_shownTag;
    QString m_picked;
    bool m_uploading = false;

    QLabel *m_note;
    QPushButton *m_new;
    QPushButton *m_refresh;
    QSplitter *m_split;
    QTreeWidget *m_list;
    QStackedWidget *m_right;  // 0: the selected release, 1: the editor
    QLabel *m_title;
    QLabel *m_meta;
    QTextBrowser *m_notes;
    QTreeWidget *m_assets;
    QBoxLayout *m_assetButtons;
    QPushButton *m_download;
    QPushButton *m_copyAsset;
    QPushButton *m_copyRelease;
    QPushButton *m_upload;
    QPushButton *m_deleteAsset;
    QBoxLayout *m_releaseButtons;
    QPushButton *m_edit;
    QPushButton *m_publish;
    QPushButton *m_deleteRelease;

    QString m_editing;  // tag of the release being edited; empty = a new one
    QLabel *m_editHeading;
    QLineEdit *m_editTag;
    QLineEdit *m_editName;
    QPlainTextEdit *m_editNotes;
    QCheckBox *m_editPre;
    QCheckBox *m_editDraft;
    QPushButton *m_editSave;
    QPushButton *m_editGenerate;
};
