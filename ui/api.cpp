#include "api.h"

#include <QJsonDocument>
#include <QNetworkReply>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QUrl>

#ifdef Q_OS_WASM
#include <emscripten/val.h>
using emscripten::val;
#endif

Api::Api(QObject *parent) : QObject(parent)
{
#ifdef Q_OS_WASM
    m_base = QString::fromStdString(val::global("location")["origin"].as<std::string>());
#else
    m_base = QProcessEnvironment::systemEnvironment().value("FILEAPI_URL", "http://127.0.0.1:8000");
#endif
    while (m_base.endsWith('/'))
        m_base.chop(1);
}

QString Api::token() const
{
#ifdef Q_OS_WASM
    val fn = val::global("fileapiToken");
    if (fn.typeOf().as<std::string>() == "function") {
        val t = fn();
        if (t.typeOf().as<std::string>() == "string")
            return QString::fromStdString(t.as<std::string>());
    }
    return {};
#else
    return QProcessEnvironment::systemEnvironment().value("FILEAPI_TOKEN");
#endif
}

QNetworkRequest Api::request(const QString &path, const QUrlQuery &query) const
{
    QUrl url(m_base + "/api/forge/" + path);
    url.setQuery(query);
    QNetworkRequest req(url);
    req.setRawHeader("Accept", "application/json");
    const QString t = token();
    if (!t.isEmpty())
        req.setRawHeader("Authorization", "Bearer " + t.toUtf8());
    return req;
}

void Api::get(const QString &path, const QUrlQuery &query, QObject *context, Callback done)
{
    finish(m_net.get(request(path, query)), context, done);
}

void Api::send(const QByteArray &method, const QString &path, const QJsonObject &body, QObject *context, Callback done)
{
    QNetworkRequest req = request(path);
    QByteArray data;
    if (!body.isEmpty()) {
        req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        data = QJsonDocument(body).toJson(QJsonDocument::Compact);
    }
    finish(m_net.sendCustomRequest(req, method, data), context, done);
}

void Api::put(const QString &path, const QUrlQuery &query, const QByteArray &data, const QByteArray &contentType,
              QObject *context, Callback done)
{
    QNetworkRequest req = request(path, query);
    req.setHeader(QNetworkRequest::ContentTypeHeader, contentType);
    finish(m_net.put(req, data), context, done);
}

void Api::finish(QNetworkReply *reply, QObject *context, Callback done)
{
    connect(reply, &QNetworkReply::finished, context, [reply, done]() {
        reply->deleteLater();
        const QByteArray data = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        QJsonObject body = QJsonDocument::fromJson(data).object();
        QString error;
        if (status >= 400 || (status == 0 && reply->error() != QNetworkReply::NoError)) {
            error = body.value("detail").toString();
            if (error.isEmpty())
                error = status ? QStringLiteral("HTTP %1").arg(status) : reply->errorString();
            body.insert(Api::StatusKey, status);  // so callers can tell "not found" from "server down"
        }
        done(body, error);
    });
}

void Api::getBytes(const QString &path, const QUrlQuery &query, QObject *context, BytesCallback done)
{
    QNetworkRequest req = request(path, query);
    req.setRawHeader("Accept", "*/*");
    finishBytes(m_net.get(req), context, done);
}

void Api::fetchExternal(const QUrl &url, QObject *context, BytesCallback done)
{
    if (url.scheme() != "https" && url.scheme() != "http")
        return done({}, QStringLiteral("unsupported URL"));
    finishBytes(m_net.get(QNetworkRequest(url)), context, done);
}

void Api::finishBytes(QNetworkReply *reply, QObject *context, BytesCallback done)
{
    connect(reply, &QNetworkReply::finished, context, [reply, done]() {
        reply->deleteLater();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (status >= 400 || reply->error() != QNetworkReply::NoError)
            return done({}, status ? QStringLiteral("HTTP %1").arg(status) : reply->errorString());
        done(reply->readAll(), QString());
    });
}

void Api::signIn()
{
#ifdef Q_OS_WASM
    val::global("fileapiSignIn")();
#endif
}

void Api::signOut()
{
#ifdef Q_OS_WASM
    val::global("fileapiSignOut")();
#endif
}

QString Api::locationHash() const
{
#ifdef Q_OS_WASM
    val location = val::global("location");
    const auto part = [&](const char *name) {
        return QUrl::fromPercentEncoding(QByteArray::fromStdString(location[name].as<std::string>()));
    };
    const QString path = part("pathname");
    if (!path.startsWith(QStringLiteral("/forge/")) || path.size() == 7)
        return {};
    QString route = path.mid(7);
    const QString search = part("search");
    if (search.startsWith(QStringLiteral("?ref=")))
        route += search;
    return route + part("hash");
#else
    return {};
#endif
}

void Api::setLocationHash(const QString &hash, bool push)
{
#ifdef Q_OS_WASM
    if (hash == locationHash())
        return;
    // <path>?ref=<ref>#L12 -> /forge/<path, percent-encoded>?ref=<ref, encoded>#L12
    QString route = hash, lines, ref;
    static const QRegularExpression linesRe(QStringLiteral("#L\\d+(?:-L?\\d+)?$"));
    const auto m = linesRe.match(route);
    if (m.hasMatch()) {
        lines = m.captured(0);
        route.chop(m.capturedLength());
    }
    const int at = route.lastIndexOf(QStringLiteral("?ref="));
    if (at >= 0) {
        ref = route.mid(at + 5);
        route.truncate(at);
    }
    QByteArray url = "/forge/" + QUrl::toPercentEncoding(route, "/!$&'()*+,;=:@");
    if (!ref.isEmpty())
        url += "?ref=" + QUrl::toPercentEncoding(ref, "/");
    url += lines.toUtf8();
    val::global("history").call<void>(push ? "pushState" : "replaceState", val::null(), std::string(""), url.toStdString());
#else
    Q_UNUSED(hash);
    Q_UNUSED(push);
#endif
}

