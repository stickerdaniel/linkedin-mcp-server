FROM ngrok/ngrok:latest

RUN ngrok --version

# Add config script (if you want to set up multiple tunnels or use diff config)
# COPY --chown=ngrok ngrok.yml /home/ngrok/.ngrok2/
COPY entrypoint.sh /

USER ngrok
ENV USER=ngrok

CMD ["/entrypoint.sh"]
