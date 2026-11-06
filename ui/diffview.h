#pragma once

#include <QJsonObject>
#include <QPlainTextEdit>
#include <QWidget>

class Api;
class QComboBox;
class QLabel;
class QPushButton;
class QToolButton;

// A commit's unified diff: file headers, hunk headers and +/- lines with coloured backgrounds, and a
// gutter with the old and new line numbers. Filled from GET /api/forge/repos/<p>/<r>/commits/<sha>.
class DiffText : public QPlainTextEdit {
public:
    explicit DiffText(QWidget *parent = nullptr);

    void setDiff(const QJsonObject &commit);
    // Block number of each file's header, in the order of the commit's "files".
    const QList<int> &fileBlocks() const { return m_fileBlocks; }
    void scrollToBlock(int block);

    int gutterWidth() const;
    void paintGutter(QPaintEvent *event);

protected:
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;

private:
    enum Kind : quint8 { FileHeader, HunkHeader, Context, Added, Removed, Note, Spacer };
    struct Line { Kind kind; int oldNo; int newNo; };
    void render();
    QColor background(Kind kind) const;

    QWidget *m_gutter;
    QJsonObject m_commit;
    QList<Line> m_lines;   // one per block
    QList<int> m_fileBlocks;
    int m_digits = 3;
};

// The Commits tab's detail page: the commit's message, author and parent, a jump-to-file list, and its diff.
class CommitView : public QWidget {
    Q_OBJECT
public:
    explicit CommitView(Api *api, QWidget *parent = nullptr);

    void setRepo(const QString &repo);
    // Loads and shows a commit (a full sha or a prefix).
    void showCommit(const QString &sha);
    // The commit asked for: as given until it loads, then the full sha.
    QString sha() const { return m_sha; }
    void setCompact(bool compact);

signals:
    void back();
    void browseFiles(const QString &sha);
    void openCommit(const QString &sha);
    void statusText(const QString &text, bool error);

private:
    void fill(const QJsonObject &commit);
    void setBodyExpanded(bool expanded);
    void fillStats();

    Api *m_api;
    QString m_repo;
    QString m_sha;
    int m_seq = 0;
    QString m_body;
    QJsonObject m_commit;
    bool m_compact = false;
    QLabel *m_title;
    QLabel *m_bodyLabel;
    QToolButton *m_more;
    QLabel *m_meta;
    QComboBox *m_files;
    QLabel *m_stats;
    QPushButton *m_browse;
    DiffText *m_diff;
};
