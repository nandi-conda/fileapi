// Small client for fileapi's forge JSON API (/api/forge/...).
#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkRequest>
#include <QObject>
#include <QUrlQuery>
#include <functional>

class Api : public QObject {
    Q_OBJECT
public:
    using Callback = std::function<void(const QJsonObject &body, const QString &error)>;

    // On an error, the callback's body carries the HTTP status (0 = no response) under this key.
    static constexpr const char *StatusKey = "_http_status";

    explicit Api(QObject *parent = nullptr);

    QString baseUrl() const { return m_base; }
    bool hasToken() const { return !token().isEmpty(); }
    QString token() const;

    // GET <base>/api/forge/<path>?<query>; `error` is empty on success.
    void get(const QString &path, const QUrlQuery &query, QObject *context, Callback done);
    // PATCH/DELETE/... <base>/api/forge/<path> with an optional JSON body.
    void send(const QByteArray &method, const QString &path, const QJsonObject &body, QObject *context, Callback done);
    // PUT <base>/api/forge/<path>?<query> with raw bytes (file uploads).
    void put(const QString &path, const QUrlQuery &query, const QByteArray &data, const QByteArray &contentType,
             QObject *context, Callback done);

    using BytesCallback = std::function<void(const QByteArray &data, const QString &error)>;
    // GET <base>/api/forge/<path>?<query> as raw bytes (file contents, e.g. images in a README).
    void getBytes(const QString &path, const QUrlQuery &query, QObject *context, BytesCallback done);
    // GET any http(s) URL, without the sign-in token (images a README links from elsewhere).
    void fetchExternal(const QUrl &url, QObject *context, BytesCallback done);

    // Hand-offs to the page's JavaScript (index.html), which runs the OAuth sign-in.
    void signIn();
    void signOut();

    // The app's route: the page URL after /forge/ (<owner>/<repo>[/<path>][?ref=<ref>][#L12], decoded), so
    // links to a repo and its tabs can be shared, and the server sees enough of them for link previews.
    // (The route used to be the URL's hash; index.html turns those old links into paths.)
    QString locationHash() const;
    // push = a new browser history entry (Back returns to the old one), else the current one is rewritten.
    // Does nothing when the URL already says this.
    void setLocationHash(const QString &hash, bool push = false);

private:
    QNetworkRequest request(const QString &path, const QUrlQuery &query = {}) const;
    void finish(QNetworkReply *reply, QObject *context, Callback done);
    void finishBytes(QNetworkReply *reply, QObject *context, BytesCallback done);

    QNetworkAccessManager m_net;
    QString m_base;
};
