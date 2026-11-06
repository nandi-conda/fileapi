#pragma once

#include <QHash>
#include <QImage>
#include <QSet>
#include <QTextBrowser>
#include <QUrl>

#include <functional>

class Api;
class QTimer;

// A rendered README or Markdown file. The server renders Markdown (GitHub-flavoured) to sanitized HTML
// (/markdown); Qt's rich-text engine lays it out, never the browser's DOM, so nothing in a README can run
// script. Relative links and images resolve inside the repo at the current ref (a leading "/" means the
// repo root, like on GitHub); images come from /raw, http(s) ones straight from the web.
class MarkdownView : public QTextBrowser {
public:
    explicit MarkdownView(Api *api, QWidget *parent = nullptr);

    // Grow to fit the whole document instead of scrolling (the README under a folder's file list, which
    // scrolls together with the list).
    void setAutoHeight(bool on);
    // Render Markdown file `path` of `repo` at `ref` (fetched from the server), then scroll to heading
    // `anchor` if given. `done(ok)` runs once it's showing, or failed to load.
    void showMarkdown(const QString &repo, const QString &ref, const QString &path, const QString &anchor = {},
                      std::function<void(bool ok)> done = {});
    // Plain text (README.txt, .rst): shown as is, wrapped.
    void showPlain(const QString &repo, const QString &ref, const QString &path, const QString &text);
    static bool isMarkdown(const QString &path);
    // Scroll to a heading by its GitHub-style anchor ("getting-started"). False when there is none.
    bool jumpTo(const QString &anchor);

    // A link to another path in the repo; `fragment` is what followed '#', or "".
    std::function<void(const QString &path, const QString &fragment)> onOpenPath;
    // Auto-height mode: scroll whatever holds this view so `y` (in this widget's coordinates) is at the top.
    std::function<void(int y)> onScrollTo;

protected:
    QVariant loadResource(int type, const QUrl &name) override;
    void resizeEvent(QResizeEvent *event) override;
    void changeEvent(QEvent *event) override;

private:
    void setFile(const QString &repo, const QString &ref, const QString &path);
    void render();
    void decorate();
    QString styleSheet() const;
    void fitHeight();
    void relayout();
    QImage fitted(const QImage &image) const;
    int imageWidth() const;
    void linkClicked(const QUrl &url);
    QUrl baseUrl() const;

    Api *m_api;
    QString m_repo;
    QString m_ref;
    QString m_path;
    QString m_content;  // HTML for Markdown, else the text
    bool m_markdown = false;
    bool m_autoHeight = false;
    bool m_rendering = false;
    int m_gen = 0;                  // drops image answers for a file that's no longer shown
    QHash<QUrl, QImage> m_images;   // as downloaded; shrunk to the view's width on display
    QSet<QUrl> m_loading;
    QSet<QUrl> m_failed;
    int m_fittedWidth = -1;         // the width the shown images were fitted to
    QTimer *m_relayout;
};
